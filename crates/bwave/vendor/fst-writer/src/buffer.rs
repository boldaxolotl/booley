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
    write_value_change_section,
};
use crate::{FstSignalId, FstSignalType, FstWriteError, Result};
use std::borrow::Cow;
use std::cmp::Ordering;
use std::io::{Cursor, Seek, Write};

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

    pub(crate) fn from_frame(
        signals: &[FstSignalType],
        frame: &[u8],
        start_time: u64,
    ) -> Result<Self> {
        let mut buffer = Self::new(signals)?;
        if frame.len() != buffer.values.len() {
            return Err(FstWriteError::InvalidFrameLength {
                expected: buffer.values.len(),
                actual: frame.len(),
            });
        }
        buffer.values.copy_from_slice(frame);
        buffer.frame.copy_from_slice(frame);
        buffer.start_time = start_time;
        buffer.end_time = start_time;
        buffer.first_buffer = false;
        write_time_chain_update(&mut buffer.time_table, 0, start_time)?;
        Ok(buffer)
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
        self.signal_change_exact_at(idx, start, value)
    }

    /// Record an already-normalized value whose length exactly matches the
    /// signal declaration. The VCD converter uses this after canonicalizing
    /// values in parser workers, avoiding the general expansion path for
    /// every event.
    pub(crate) fn signal_change_exact(
        &mut self,
        signal_id: FstSignalId,
        value: &[u8],
    ) -> Result<()> {
        let idx = signal_id.to_array_index();
        let info = match self.signals.get(idx) {
            Some(info) => info,
            None => return Err(FstWriteError::InvalidSignalId(signal_id)),
        };
        if value.len() != info.len as usize {
            return Err(FstWriteError::InvalidFrameLength {
                expected: info.len as usize,
                actual: value.len(),
            });
        }
        self.signal_change_exact_at(idx, info.offset as usize, value)
    }

    #[inline]
    fn signal_change_exact_at(&mut self, idx: usize, start: usize, value: &[u8]) -> Result<()> {
        let len = value.len();
        let first_time_step = self.time_table.is_empty();
        if first_time_step && self.first_buffer {
            self.values[start..start + len].copy_from_slice(value);
        } else {
            if self.time_table.is_empty() {
                todo!("Currently we only support flushing right before a new time step.")
            }

            // check to see if there actually was a change
            if &self.values[start..start + len] == value {
                return Ok(());
            }
            self.values[start..start + len].copy_from_slice(value);
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

    fn num_time_table_entries(&self) -> u64 {
        if self.time_table.is_empty() {
            0
        } else {
            self.time_table_index as u64 + 1
        }
    }

    pub(crate) fn flush(&mut self, output: &mut (impl Write + Seek)) -> Result<u64> {
        // A timestamp-aligned section may end before a later time change
        // (notably when the first timestamp contains one very wide value).
        // Ordinarily `time_change` snapshots first-step values into `frame`;
        // do it here as well when this section contains only that step.
        if self.first_buffer && self.time_table.is_empty() {
            self.frame.copy_from_slice(&self.values);
        }
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

    pub(crate) fn encode(&mut self) -> Result<(Vec<u8>, u64)> {
        let mut output = Cursor::new(Vec::with_capacity(self.size()));
        let end_time = self.flush(&mut output)?;
        Ok((output.into_inner(), end_time))
    }

    /// Returns the estimated size of all data structures that grow over time.
    pub(crate) fn size(&self) -> usize {
        self.time_table.len() + self.value_changes_bytes
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
