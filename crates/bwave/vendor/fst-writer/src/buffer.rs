// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>
//
// bwave vendored change: value changes are buffered in one chronological
// `Vec<u8>` per signal (already in final FST block encoding) instead of the
// upstream single-vec backward-linked lists. Appends encode straight into
// the signal's stream and flushing borrows it zero-copy, where upstream paid
// two backward pointer-chases over the whole buffer per signal at flush time
// plus a 4-byte back-pointer and a length varint per change.

use crate::io::{
    write_multi_bit_signal, write_one_bit_signal, write_time_chain_update,
    write_value_change_section, write_variant_u64,
};
use crate::{FstSignalId, FstSignalType, FstWriteError, Result};
use std::borrow::Cow;
use std::cmp::Ordering;
use std::io::{Seek, Write};

/// keeps track of signal values before writing them to disk
pub(crate) struct SignalBuffer {
    start_time: u64,
    end_time: u64,
    /// constant signal meta-data
    signals: Vec<SignalInfo>,
    /// time table index of the previous change for each signal
    prev_time_table_index: Box<[u32]>,
    /// values for all signals in the first time step of this block
    frame: Box<[u8]>,
    /// copy of the frame with all value changes applied
    values: Box<[u8]>,
    /// per-signal chronological value-change streams in final FST encoding
    value_changes: Vec<Vec<u8>>,
    /// running total of buffered value-change bytes (cheap `size()`)
    value_changes_bytes: usize,
    /// contains the delta encoded and compressed timetable
    time_table: Vec<u8>,
    time_table_index: u32,
    /// is this the first buffer for the file that we are writing?
    first_buffer: bool,
}

#[derive(Debug, Clone)]
struct SignalInfo {
    /// length in bytes / number of characters
    len: u32,
    /// starting offset in the value buffer
    offset: u32,
}

fn gen_signal_info(signals: &[FstSignalType]) -> (Vec<SignalInfo>, usize) {
    let mut offset = 0;
    let mut out = Vec::with_capacity(signals.len());
    for signal in signals {
        out.push(SignalInfo {
            len: signal.len(),
            offset,
        });
        offset += signal.len();
    }
    (out, offset as usize)
}

impl SignalBuffer {
    pub(crate) fn new(signals: &[FstSignalType]) -> Result<Self> {
        let (signals, values_len) = gen_signal_info(signals);
        let value_changes = vec![Vec::new(); signals.len()];
        let values = vec![b'x'; values_len].into_boxed_slice();
        let frame = values.clone();
        let prev_time_table_index = vec![0; signals.len()].into_boxed_slice();
        let time_table = Vec::with_capacity(16);
        Ok(Self {
            start_time: 0,
            end_time: 0,
            signals,
            prev_time_table_index,
            frame,
            values,
            value_changes,
            value_changes_bytes: 0,
            time_table,
            time_table_index: 0,
            first_buffer: true,
        })
    }

    pub(crate) fn time_change(&mut self, new_time: u64) -> Result<()> {
        match new_time.cmp(&self.end_time) {
            Ordering::Less => Err(FstWriteError::TimeDecrease(self.end_time, new_time)),
            Ordering::Equal => Ok(()),
            Ordering::Greater => {
                let first_time_step = self.time_table.is_empty();
                if first_time_step {
                    // at the end of the first step, we copy values over into the frame
                    self.frame = self.values.clone();
                } else {
                    // the first step is not captured in the time table, but instead in the start_time
                    self.time_table_index += 1;
                }
                debug_assert!(self.start_time <= self.end_time);

                // in the first step, the time needs to be written relative to 0
                let delta_to = if first_time_step { 0 } else { self.end_time };
                // write timetable in compressed format
                write_time_chain_update(&mut self.time_table, delta_to, new_time)?;
                self.end_time = new_time;
                Ok(())
            }
        }
    }

    pub(crate) fn signal_change(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        let idx = signal_id.to_array_index();
        let info = match self.signals.get(idx) {
            Some(info) => info,
            None => return Err(FstWriteError::InvalidSignalId(signal_id)),
        };
        let len = info.len as usize;
        let start = info.offset as usize;
        let range = start..start + len;
        let value_cow = if value.len() == len {
            Cow::Borrowed(value)
        } else {
            let expanded = expand_special_vector_cases(value, len).unwrap_or_else(|| {
                panic!(
                    "Failed to parse four state value: {} for signal of size {}",
                    String::from_utf8_lossy(value),
                    len
                )
            });
            assert_eq!(expanded.len(), len);
            Cow::Owned(expanded)
        };
        let value = value_cow.as_ref();
        debug_assert_eq!(value.len(), len);
        let first_time_step = self.time_table.is_empty();
        if first_time_step && self.first_buffer {
            self.values[range].copy_from_slice(value);
        } else {
            if self.time_table.is_empty() {
                todo!("Currently we only support flushing right before a new time step.")
            }

            // check to see if there actually was a change
            if &self.values[range.clone()] == value {
                return Ok(());
            }
            self.values[range].copy_from_slice(value);
            // append the change to the signal's chronological stream
            let time_table_idx_delta =
                (self.time_table_index - self.prev_time_table_index[idx]) as u64;
            let stream = &mut self.value_changes[idx];
            let before = stream.len();
            match value {
                [value] => write_one_bit_signal(stream, time_table_idx_delta, *value)?,
                values => write_multi_bit_signal(stream, time_table_idx_delta, values)?,
            }
            self.value_changes_bytes += stream.len() - before;

            // remember previous time-table index
            self.prev_time_table_index[idx] = self.time_table_index;
        }
        Ok(())
    }

    pub(crate) fn signal_change_vcd(&mut self, signal_id: FstSignalId, raw: &[u8]) -> Result<()> {
        let idx = signal_id.to_array_index();
        let info = self
            .signals
            .get(idx)
            .ok_or(FstWriteError::InvalidSignalId(signal_id))?;
        let start = info.offset as usize;
        let normalized = NormalizedVcd::new(raw, info.len as usize);
        let current = &mut self.values[start..start + info.len as usize];
        if self.time_table.is_empty() && self.first_buffer {
            normalized.copy_into(current);
            return Ok(());
        }
        if self.time_table.is_empty() {
            todo!("Currently we only support flushing right before a new time step.")
        }

        let delta = (self.time_table_index - self.prev_time_table_index[idx]) as u64;
        let stream = &mut self.value_changes[idx];
        let before = stream.len();
        if normalized.append_change(stream, current, delta)? {
            self.value_changes_bytes += stream.len() - before;
            self.prev_time_table_index[idx] = self.time_table_index;
        }
        Ok(())
    }

    fn num_time_table_entries(&self) -> u64 {
        if self.time_table.is_empty() {
            0
        } else {
            self.time_table_index as u64 + 1
        }
    }

    pub(crate) fn flush(&mut self, output: &mut (impl Write + Seek)) -> Result<u64> {
        // write data
        write_value_change_section(
            output,
            self.start_time,
            self.end_time,
            &self.frame,
            &self.time_table,
            self.num_time_table_entries(),
            &self.value_changes,
        )?;

        // reset data
        self.time_table_index = 0;
        for idx in self.prev_time_table_index.iter_mut() {
            *idx = 0;
        }
        self.start_time = self.end_time;
        self.time_table.clear();
        for stream in self.value_changes.iter_mut() {
            stream.clear();
        }
        self.value_changes_bytes = 0;
        self.first_buffer = false;

        Ok(self.end_time)
    }

    /// Returns the estimated size of all data structures that grow over time.
    pub(crate) fn size(&self) -> usize {
        self.time_table.len() + self.value_changes_bytes
    }
}

struct NormalizedVcd<'a> {
    source: &'a [u8],
    prefix: usize,
    fill: u8,
}

impl<'a> NormalizedVcd<'a> {
    fn new(raw: &'a [u8], width: usize) -> Self {
        let source = if raw.len() > width {
            &raw[raw.len() - width..]
        } else {
            raw
        };
        let fill = match raw.first().copied().unwrap_or(b'0').to_ascii_lowercase() {
            b'x' => b'x',
            b'z' => b'z',
            _ => b'0',
        };
        Self {
            source,
            prefix: width - source.len(),
            fill,
        }
    }

    #[inline(always)]
    fn byte(&self, index: usize) -> u8 {
        if index < self.prefix {
            self.fill
        } else {
            self.source[index - self.prefix].to_ascii_lowercase()
        }
    }

    fn copy_into(&self, current: &mut [u8]) {
        for (index, slot) in current.iter_mut().enumerate() {
            *slot = self.byte(index);
        }
    }

    fn inspect(&self, current: &[u8]) -> (bool, bool) {
        let mut changed = false;
        let mut two_state = true;
        for (index, old) in current.iter().enumerate() {
            let value = self.byte(index);
            changed |= *old != value;
            two_state &= matches!(value, b'0' | b'1');
        }
        (changed, two_state)
    }

    fn append_change(&self, stream: &mut Vec<u8>, current: &mut [u8], delta: u64) -> Result<bool> {
        if let Some(changed) = self.exact_two_state_changed(current) {
            if !changed {
                return Ok(false);
            }
            if current.len() == 1 {
                current[0] = self.source[0];
                write_one_bit_signal(stream, delta, self.source[0])?;
            } else {
                write_variant_u64(stream, delta << 1)?;
                self.append_exact_two_state(stream, current);
            }
            return Ok(true);
        }

        let (changed, two_state) = self.inspect(current);
        if !changed {
            return Ok(false);
        }
        if current.len() == 1 {
            let value = self.byte(0);
            current[0] = value;
            write_one_bit_signal(stream, delta, value)?;
        } else {
            write_variant_u64(stream, (delta << 1) | (!two_state as u64))?;
            if two_state {
                self.append_two_state(stream, current);
            } else {
                self.append_four_state(stream, current);
            }
        }
        Ok(true)
    }

    fn exact_two_state_changed(&self, current: &[u8]) -> Option<bool> {
        if self.prefix != 0 {
            return None;
        }
        let mut changed = false;
        for (&old, &value) in current.iter().zip(self.source) {
            if !matches!(value, b'0' | b'1') {
                return None;
            }
            changed |= old != value;
        }
        Some(changed)
    }

    fn append_exact_two_state(&self, stream: &mut Vec<u8>, current: &mut [u8]) {
        let mut packed = 0u8;
        for (index, (slot, &value)) in current.iter_mut().zip(self.source).enumerate() {
            *slot = value;
            packed |= (value - b'0') << (7 - (index & 7));
            if index & 7 == 7 {
                stream.push(packed);
                packed = 0;
            }
        }
        if current.len() & 7 != 0 {
            stream.push(packed);
        }
    }

    fn append_two_state(&self, stream: &mut Vec<u8>, current: &mut [u8]) {
        let mut packed = 0u8;
        for (index, slot) in current.iter_mut().enumerate() {
            let value = self.byte(index);
            *slot = value;
            packed |= (value - b'0') << (7 - (index & 7));
            if index & 7 == 7 {
                stream.push(packed);
                packed = 0;
            }
        }
        if current.len() & 7 != 0 {
            stream.push(packed);
        }
    }

    fn append_four_state(&self, stream: &mut Vec<u8>, current: &mut [u8]) {
        for (index, slot) in current.iter_mut().enumerate() {
            let value = self.byte(index);
            *slot = value;
            stream.push(value);
        }
    }
}

/// tries to expand common shortenings used in VCD encodings
#[inline]
fn expand_special_vector_cases(value: &[u8], len: usize) -> Option<Vec<u8>> {
    // if the value is actually longer than expected, there is nothing we can do
    if value.len() >= len {
        return None;
    }

    // zero, x or z extend
    match value[0] {
        b'1' | b'0' => {
            let mut extended = Vec::with_capacity(len);
            extended.resize(len - value.len(), b'0');
            extended.extend_from_slice(value);
            Some(extended)
        }
        b'x' | b'X' | b'z' | b'Z' => {
            let mut extended = Vec::with_capacity(len);
            extended.resize(len - value.len(), value[0]);
            extended.extend_from_slice(value);
            Some(extended)
        }
        _ => None, // failed
    }
}

#[cfg(test)]
mod tests {
    use super::NormalizedVcd;
    use crate::io::write_multi_bit_signal;

    fn normalize(raw: &[u8], width: usize) -> Vec<u8> {
        let mut current = vec![b'?'; width];
        NormalizedVcd::new(raw, width).copy_into(&mut current);
        current
    }

    #[test]
    fn vcd_normalization_handles_width_and_four_state_values() {
        assert_eq!(normalize(b"101", 8), b"00000101");
        assert_eq!(normalize(b"X1", 4), b"xxx1");
        assert_eq!(normalize(b"Z", 4), b"zzzz");
        assert_eq!(normalize(b"101011", 4), b"1011");
        assert_eq!(normalize(b"", 4), b"0000");
    }

    #[test]
    fn exact_two_state_encoding_matches_generic_encoding() {
        let value = b"10101100";
        let delta = 7;
        let mut current = vec![b'x'; value.len()];
        let mut specialized = Vec::new();
        assert!(
            NormalizedVcd::new(value, value.len())
                .append_change(&mut specialized, &mut current, delta)
                .unwrap()
        );

        let mut generic = Vec::new();
        write_multi_bit_signal(&mut generic, delta, value).unwrap();
        assert_eq!(specialized, generic);
        assert_eq!(current, value);
        assert!(
            !NormalizedVcd::new(value, value.len())
                .append_change(&mut specialized, &mut current, delta)
                .unwrap()
        );
    }
}
