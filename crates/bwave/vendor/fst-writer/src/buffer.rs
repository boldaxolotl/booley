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
    MIN_SIZE_TO_ATTEMPT_COMPRESSION, write_multi_bit_signal, write_one_bit_signal,
    write_packed_binary_signal, write_time_chain_update, write_value_change_section,
    write_variant_u64,
};
use crate::writer::{
    FST_FRAME_TIME_INDEX, FST_NO_CHANGE, FstDumpState, FstSignalChange, FstSignalRecord,
};
use crate::{FstSignalId, FstSignalType, FstWriteError, Result};
use rayon::prelude::*;
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

struct ChainedSignalState {
    frame: Vec<u8>,
    current: Vec<u8>,
    previous_time_index: u32,
    pending_record: Option<u32>,
    pending_frame_record: Option<u32>,
}

pub(crate) struct ChainedSignalBuffer {
    start_time: u64,
    end_time: u64,
    time_table: Vec<u8>,
    time_table_index: u32,
    first_file_section: bool,
    signals: Vec<ChainedSignalState>,
    value_changes: Vec<Vec<u8>>,
    value_changes_bytes: usize,
    pack_cpu_seconds: f64,
    worker_cpu_seconds: Vec<f64>,
    arena_to_packer_copied_bytes: usize,
}

pub(crate) struct ChainedBufferStats {
    pub(crate) pack_cpu_seconds: f64,
    pub(crate) worker_cpu_seconds: Vec<f64>,
    pub(crate) arena_to_packer_copied_bytes: usize,
    pub(crate) packer_to_compressor_bytes: usize,
}

struct ApplySignalStats {
    encoded_bytes: usize,
    copied_bytes: usize,
    cpu_seconds: f64,
    worker_index: usize,
}

/// Parser-produced changes for one signal in one timestamp-led chunk.
///
/// The first two distinct values remain explicit because the first value may
/// match section state. Later values are already in final FST encoding and can
/// normally be appended without decoding or copying.
#[derive(Default)]
pub struct FstSignalFragment {
    first: Option<FstSignalRecord>,
    first_time_last: Option<FstSignalRecord>,
    second: Option<FstSignalRecord>,
    last: Option<FstSignalRecord>,
    tail: Vec<u8>,
    change_count: usize,
}

impl FstSignalFragment {
    pub fn reset(&mut self, first: FstSignalRecord) {
        if self.tail.capacity() == 0 {
            self.tail.reserve(128);
        }
        self.first = Some(first);
        self.first_time_last = Some(first);
        self.second = None;
        self.last = Some(first);
        self.tail.clear();
        self.change_count = 1;
    }

    pub fn signal(&self) -> u32 {
        self.first.expect("initialized signal fragment").signal()
    }

    pub fn last(&self) -> FstSignalRecord {
        self.last.expect("initialized signal fragment")
    }

    pub fn last_inline_value(&self) -> Option<u8> {
        let last = self.last.expect("initialized signal fragment");
        last.is_inline().then(|| last.inline_value())
    }

    pub fn change_count(&self) -> usize {
        self.change_count
    }

    pub fn representation_bytes(&self) -> usize {
        std::mem::size_of::<Self>() + self.tail.len()
    }

    pub fn tail_capacity_bytes(&self) -> usize {
        self.tail.capacity()
    }

    pub fn push(&mut self, change: FstSignalRecord, width: usize, values: &[u8]) -> Result<()> {
        let previous = self.last.expect("initialized signal fragment");
        if change.signal() != previous.signal() {
            return Err(FstWriteError::InvalidSignalChanges(
                "fragment contains changes for multiple signals".to_string(),
            ));
        }
        let value = exact_change_value(&change, width, values)?;
        let old_value = exact_change_value(&previous, width, values)?;
        if value.equals_value(&old_value) {
            return Ok(());
        }
        self.change_count += 1;
        if change.time_index()
            == self
                .first
                .expect("initialized signal fragment")
                .time_index()
        {
            self.first_time_last = Some(change);
        }
        if self.second.is_none() {
            self.second = Some(change);
        } else {
            let delta = change
                .time_index()
                .checked_sub(previous.time_index())
                .ok_or_else(|| {
                    FstWriteError::InvalidSignalChanges(
                        "chunk fragment time index decreased".to_string(),
                    )
                })?;
            value.write_to(&mut self.tail, u64::from(delta))?;
        }
        self.last = Some(change);
        Ok(())
    }
}

#[cfg(feature = "profile")]
fn thread_cpu_seconds() -> f64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `value` points to writable storage for the duration of the call.
    let result = unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut value) };
    if result == 0 {
        value.tv_sec as f64 + value.tv_nsec as f64 / 1_000_000_000.0
    } else {
        0.0
    }
}

#[cfg(not(feature = "profile"))]
fn thread_cpu_seconds() -> f64 {
    0.0
}

enum ExactChangeValue<'a> {
    Inline(u8),
    Arena(&'a [u8]),
    PackedBinary { bytes: &'a [u8], width: usize },
}

impl ExactChangeValue<'_> {
    fn len(&self) -> usize {
        match self {
            Self::Inline(_) => 1,
            Self::Arena(value) => value.len(),
            Self::PackedBinary { width, .. } => *width,
        }
    }

    fn equals(&self, current: &[u8]) -> bool {
        match self {
            Self::Inline(value) => current == std::slice::from_ref(value),
            Self::Arena(value) => current == *value,
            Self::PackedBinary { bytes, width } => {
                current.len() == *width
                    && current.iter().enumerate().all(|(index, current)| {
                        let bit = (bytes[index / 8] >> (7 - (index & 7))) & 1;
                        *current == b'0' + bit
                    })
            }
        }
    }

    fn copy_to(&self, output: &mut [u8]) {
        match self {
            Self::Inline(value) => output.copy_from_slice(std::slice::from_ref(value)),
            Self::Arena(value) => output.copy_from_slice(value),
            Self::PackedBinary { bytes, width } => {
                debug_assert_eq!(output.len(), *width);
                for (index, output) in output.iter_mut().enumerate() {
                    let bit = (bytes[index / 8] >> (7 - (index & 7))) & 1;
                    *output = b'0' + bit;
                }
            }
        }
    }

    fn equals_value(&self, other: &Self) -> bool {
        match (self, other) {
            (
                Self::PackedBinary {
                    bytes: left,
                    width: left_width,
                },
                Self::PackedBinary {
                    bytes: right,
                    width: right_width,
                },
            ) => left_width == right_width && left == right,
            (_, Self::Inline(value)) => self.equals(std::slice::from_ref(value)),
            (_, Self::Arena(value)) => self.equals(value),
            (Self::Inline(value), Self::PackedBinary { .. }) => {
                other.equals(std::slice::from_ref(value))
            }
            (Self::Arena(value), Self::PackedBinary { .. }) => other.equals(value),
        }
    }

    fn write_to(&self, stream: &mut Vec<u8>, delta: u64) -> Result<()> {
        match self {
            Self::Inline(value) => write_one_bit_signal(stream, delta, *value),
            Self::Arena(value) if value.len() == 1 => write_one_bit_signal(stream, delta, value[0]),
            Self::Arena(value) => write_multi_bit_signal(stream, delta, value),
            Self::PackedBinary { bytes, width: 1 } => {
                write_one_bit_signal(stream, delta, b'0' + (bytes[0] >> 7))
            }
            Self::PackedBinary { bytes, .. } => write_packed_binary_signal(stream, delta, bytes),
        }
    }
}

fn exact_change_value<'a>(
    change: &FstSignalRecord,
    len: usize,
    values: &'a [u8],
) -> Result<ExactChangeValue<'a>> {
    if change.is_inline() {
        if len != 1 {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "inline value used for {}-byte signal {}",
                len,
                change.signal()
            )));
        }
        return Ok(ExactChangeValue::Inline(change.inline_value()));
    }
    let start = change.value_offset() as usize;
    let stored_len = if change.is_packed_binary() {
        len.div_ceil(8)
    } else {
        len
    };
    let end = start.checked_add(stored_len).ok_or_else(|| {
        FstWriteError::InvalidSignalChanges("value range exceeds usize".to_string())
    })?;
    values.get(start..end).map_or_else(
        || {
            Err(FstWriteError::InvalidSignalChanges(format!(
                "value range {start}..{end} is outside the arena"
            )))
        },
        |value| {
            Ok(if change.is_packed_binary() {
                ExactChangeValue::PackedBinary {
                    bytes: value,
                    width: len,
                }
            } else {
                ExactChangeValue::Arena(value)
            })
        },
    )
}

#[allow(clippy::too_many_arguments)]
fn apply_signal_chain(
    signal_index: usize,
    state: &mut ChainedSignalState,
    stream: &mut Vec<u8>,
    first_record: u32,
    changes: &[FstSignalChange],
    values: &[u8],
    max_time_index: u32,
    first_file_section: bool,
) -> Result<ApplySignalStats> {
    let cpu_started = thread_cpu_seconds();
    let initial_stream_len = stream.len();
    let mut copied_bytes = 0usize;
    let mut current_record = None;
    let mut frame_record = None;
    let mut record_index = first_record;
    let mut visited = 0usize;
    while record_index != FST_NO_CHANGE {
        if visited >= changes.len() {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "cycle in chain for signal {signal_index}"
            )));
        }
        visited += 1;
        let this_record = record_index;
        let change = changes.get(this_record as usize).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges(format!(
                "record {record_index} for signal {signal_index} is out of bounds"
            ))
        })?;
        record_index = change.next();
        if change.signal() as usize != signal_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "record belongs to signal {}, expected {signal_index}",
                change.signal()
            )));
        }
        if change.dump_state() != FstDumpState::Enabled {
            continue;
        }
        let value = exact_change_value(change.record(), state.current.len(), values)?;
        if change.time_index() == FST_FRAME_TIME_INDEX {
            if !first_file_section {
                return Err(FstWriteError::InvalidSignalChanges(format!(
                    "frame-time change in noninitial section for signal {signal_index}"
                )));
            }
            current_record = Some(this_record);
            frame_record = Some(this_record);
            continue;
        }
        if change.time_index() > max_time_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index {} for signal {signal_index} exceeds {max_time_index}",
                change.time_index()
            )));
        }
        if change.time_index() < state.previous_time_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index decreased in signal {signal_index}"
            )));
        }
        let unchanged = match current_record {
            Some(index) => {
                let current = exact_change_value(
                    changes[index as usize].record(),
                    state.current.len(),
                    values,
                )?;
                value.equals_value(&current)
            }
            None => value.equals(&state.current),
        };
        if unchanged {
            continue;
        }
        let delta = u64::from(change.time_index() - state.previous_time_index);
        value.write_to(stream, delta)?;
        state.previous_time_index = change.time_index();
        current_record = Some(this_record);
    }
    if let Some(index) = current_record {
        let value = exact_change_value(
            changes[index as usize].record(),
            state.current.len(),
            values,
        )?;
        value.copy_to(&mut state.current);
        copied_bytes += value.len();
    }
    if let Some(index) = frame_record {
        let value =
            exact_change_value(changes[index as usize].record(), state.frame.len(), values)?;
        value.copy_to(&mut state.frame);
        copied_bytes += value.len();
    }
    Ok(ApplySignalStats {
        encoded_bytes: stream.len() - initial_stream_len,
        copied_bytes,
        cpu_seconds: thread_cpu_seconds() - cpu_started,
        worker_index: rayon::current_thread_index().unwrap_or(0),
    })
}

#[allow(clippy::too_many_arguments)]
fn apply_signal_range(
    range_start: usize,
    states: &mut [ChainedSignalState],
    streams: &mut [Vec<u8>],
    changes: &[FstSignalRecord],
    values: &[u8],
    max_time_index: u32,
    first_file_section: bool,
) -> Result<ApplySignalStats> {
    let cpu_started = thread_cpu_seconds();
    let initial_stream_bytes = streams.iter().map(Vec::len).sum::<usize>();
    let range_end = range_start + states.len();
    for (record_index, change) in changes.iter().enumerate() {
        let signal_index = change.signal() as usize;
        if signal_index < range_start || signal_index >= range_end {
            continue;
        }
        if change.dump_state() != FstDumpState::Enabled {
            continue;
        }
        let local = signal_index - range_start;
        let state = &mut states[local];
        let stream = &mut streams[local];
        let value = exact_change_value(change, state.current.len(), values)?;
        let record_index = u32::try_from(record_index).map_err(|_| {
            FstWriteError::InvalidSignalChanges("chunk change table exceeds u32".to_string())
        })?;
        if change.time_index() == FST_FRAME_TIME_INDEX {
            if !first_file_section {
                return Err(FstWriteError::InvalidSignalChanges(format!(
                    "frame-time change in noninitial section for signal {signal_index}"
                )));
            }
            state.pending_record = Some(record_index);
            state.pending_frame_record = Some(record_index);
            continue;
        }
        if change.time_index() > max_time_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index {} for signal {signal_index} exceeds {max_time_index}",
                change.time_index()
            )));
        }
        if change.time_index() < state.previous_time_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index decreased in signal {signal_index}"
            )));
        }
        let unchanged = match state.pending_record {
            Some(index) => {
                let current =
                    exact_change_value(&changes[index as usize], state.current.len(), values)?;
                value.equals_value(&current)
            }
            None => value.equals(&state.current),
        };
        if unchanged {
            continue;
        }
        let delta = u64::from(change.time_index() - state.previous_time_index);
        value.write_to(stream, delta)?;
        state.previous_time_index = change.time_index();
        state.pending_record = Some(record_index);
    }
    let mut copied_bytes = 0usize;
    for state in states {
        if let Some(index) = state.pending_record.take() {
            let value = exact_change_value(&changes[index as usize], state.current.len(), values)?;
            value.copy_to(&mut state.current);
            copied_bytes += value.len();
        }
        if let Some(index) = state.pending_frame_record.take() {
            let value = exact_change_value(&changes[index as usize], state.frame.len(), values)?;
            value.copy_to(&mut state.frame);
            copied_bytes += value.len();
        }
    }
    Ok(ApplySignalStats {
        encoded_bytes: streams.iter().map(Vec::len).sum::<usize>() - initial_stream_bytes,
        copied_bytes,
        cpu_seconds: thread_cpu_seconds() - cpu_started,
        worker_index: rayon::current_thread_index().unwrap_or(0),
    })
}

fn fragment_time_index(record: FstSignalRecord, time_map: &[u32]) -> Result<u32> {
    time_map
        .get(record.time_index() as usize)
        .copied()
        .ok_or_else(|| {
            FstWriteError::InvalidSignalChanges(format!(
                "fragment time index {} is outside the chunk map",
                record.time_index()
            ))
        })
}

fn read_variant_u64(bytes: &[u8], position: &mut usize) -> Result<u64> {
    let mut value = 0u64;
    for shift in (0..70).step_by(7) {
        let byte = *bytes.get(*position).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges("truncated fragment varint".to_string())
        })?;
        *position += 1;
        value |= u64::from(byte & 0x7f) << shift;
        if byte & 0x80 == 0 {
            return Ok(value);
        }
    }
    Err(FstWriteError::InvalidSignalChanges(
        "fragment varint exceeds u64".to_string(),
    ))
}

fn rewrite_fragment_tail(
    output: &mut Vec<u8>,
    tail: &[u8],
    width: usize,
    mut local_time: u32,
    mut previous_time: u32,
    time_map: &[u32],
) -> Result<u32> {
    let mut position = 0usize;
    while position < tail.len() {
        let header = read_variant_u64(tail, &mut position)?;
        let shift = if width == 1 && header & 1 == 0 {
            2
        } else if width == 1 {
            4
        } else {
            1
        };
        let local_delta = u32::try_from(header >> shift).map_err(|_| {
            FstWriteError::InvalidSignalChanges("fragment time delta exceeds u32".to_string())
        })?;
        local_time = local_time.checked_add(local_delta).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges("fragment time index exceeds u32".to_string())
        })?;
        let mapped_time = *time_map.get(local_time as usize).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges(format!(
                "fragment time index {local_time} is outside the chunk map"
            ))
        })?;
        if mapped_time == FST_FRAME_TIME_INDEX || mapped_time < previous_time {
            return Err(FstWriteError::InvalidSignalChanges(
                "fragment mapped time is not chronological".to_string(),
            ));
        }
        let preserved = header & ((1 << shift) - 1);
        write_variant_u64(
            output,
            (u64::from(mapped_time - previous_time) << shift) | preserved,
        )?;
        let payload_len = if width == 1 {
            0
        } else if header & 1 == 0 {
            width.div_ceil(8)
        } else {
            width
        };
        let end = position.checked_add(payload_len).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges("fragment payload exceeds usize".to_string())
        })?;
        let payload = tail.get(position..end).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges("truncated fragment payload".to_string())
        })?;
        output.extend_from_slice(payload);
        position = end;
        previous_time = mapped_time;
    }
    Ok(previous_time)
}

fn apply_signal_fragment(
    signal_index: usize,
    state: &mut ChainedSignalState,
    stream: &mut Vec<u8>,
    fragment: &FstSignalFragment,
    values: &[u8],
    time_map: &[u32],
    time_map_is_affine: bool,
    first_file_section: bool,
) -> Result<usize> {
    let first = fragment.first.expect("initialized signal fragment");
    let first_value = exact_change_value(&first, state.current.len(), values)?;
    let first_time = fragment_time_index(first, time_map)?;
    if first_time == FST_FRAME_TIME_INDEX {
        if !first_file_section {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "frame-time fragment in noninitial section for signal {signal_index}"
            )));
        }
    } else if !first_value.equals(&state.current) {
        first_value.write_to(stream, u64::from(first_time - state.previous_time_index))?;
        state.previous_time_index = first_time;
    }

    if let Some(second) = fragment.second {
        let second_value = exact_change_value(&second, state.current.len(), values)?;
        let second_time = fragment_time_index(second, time_map)?;
        if second_time == FST_FRAME_TIME_INDEX {
            if !first_file_section {
                return Err(FstWriteError::InvalidSignalChanges(format!(
                    "frame-time fragment in noninitial section for signal {signal_index}"
                )));
            }
        } else {
            second_value.write_to(stream, u64::from(second_time - state.previous_time_index))?;
            state.previous_time_index = second_time;
        }
        if !fragment.tail.is_empty() {
            let last = fragment.last.expect("initialized signal fragment");
            let last_time = fragment_time_index(last, time_map)?;
            if time_map_is_affine {
                stream.extend_from_slice(&fragment.tail);
                state.previous_time_index = last_time;
            } else {
                state.previous_time_index = rewrite_fragment_tail(
                    stream,
                    &fragment.tail,
                    state.current.len(),
                    second.time_index(),
                    state.previous_time_index,
                    time_map,
                )?;
            }
        }
    }

    let last = fragment.last.expect("initialized signal fragment");
    let last_value = exact_change_value(&last, state.current.len(), values)?;
    last_value.copy_to(&mut state.current);
    let mut copied_bytes = last_value.len();
    let first_time_last = fragment
        .first_time_last
        .expect("initialized signal fragment");
    if fragment_time_index(first_time_last, time_map)? == FST_FRAME_TIME_INDEX {
        let frame_value = exact_change_value(&first_time_last, state.frame.len(), values)?;
        frame_value.copy_to(&mut state.frame);
        copied_bytes += frame_value.len();
    }
    Ok(copied_bytes)
}

#[allow(clippy::too_many_arguments)]
fn apply_fragment_range(
    range_start: usize,
    states: &mut [ChainedSignalState],
    streams: &mut [Vec<u8>],
    fragments: &[FstSignalFragment],
    fragment_slots: &[u32],
    values: &[u8],
    time_map: &[u32],
    time_map_is_affine: bool,
    first_file_section: bool,
) -> Result<ApplySignalStats> {
    let cpu_started = thread_cpu_seconds();
    let initial_stream_bytes = streams.iter().map(Vec::len).sum::<usize>();
    let mut copied_bytes = 0usize;
    for (local, (state, stream)) in states.iter_mut().zip(streams.iter_mut()).enumerate() {
        let signal_index = range_start + local;
        let fragment_index = fragment_slots[signal_index];
        if fragment_index == FST_NO_CHANGE {
            continue;
        }
        let fragment = fragments.get(fragment_index as usize).ok_or_else(|| {
            FstWriteError::InvalidSignalChanges(format!(
                "fragment {fragment_index} for signal {signal_index} is out of bounds"
            ))
        })?;
        if fragment.signal() as usize != signal_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "fragment belongs to signal {}, expected {signal_index}",
                fragment.signal()
            )));
        }
        copied_bytes += apply_signal_fragment(
            signal_index,
            state,
            stream,
            fragment,
            values,
            time_map,
            time_map_is_affine,
            first_file_section,
        )?;
    }
    Ok(ApplySignalStats {
        encoded_bytes: streams.iter().map(Vec::len).sum::<usize>() - initial_stream_bytes,
        copied_bytes,
        cpu_seconds: thread_cpu_seconds() - cpu_started,
        worker_index: rayon::current_thread_index().unwrap_or(0),
    })
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

impl ChainedSignalBuffer {
    pub(crate) fn new(signals: &[FstSignalType]) -> Result<Self> {
        let states = signals
            .iter()
            .map(|signal| {
                let value = vec![b'x'; signal.len() as usize];
                ChainedSignalState {
                    frame: value.clone(),
                    current: value,
                    previous_time_index: 0,
                    pending_record: None,
                    pending_frame_record: None,
                }
            })
            .collect::<Vec<_>>();
        Ok(Self {
            start_time: 0,
            end_time: 0,
            time_table: Vec::with_capacity(16),
            time_table_index: 0,
            first_file_section: true,
            value_changes: vec![Vec::new(); states.len()],
            signals: states,
            value_changes_bytes: 0,
            pack_cpu_seconds: 0.0,
            worker_cpu_seconds: Vec::new(),
            arena_to_packer_copied_bytes: 0,
        })
    }

    pub(crate) fn from_frame(
        signals: &[FstSignalType],
        frame: &[u8],
        start_time: u64,
    ) -> Result<Self> {
        let expected = signals.iter().map(|signal| signal.len() as usize).sum();
        if frame.len() != expected {
            return Err(FstWriteError::InvalidFrameLength {
                expected,
                actual: frame.len(),
            });
        }
        let mut offset = 0usize;
        let states = signals
            .iter()
            .map(|signal| {
                let end = offset + signal.len() as usize;
                let value = frame[offset..end].to_vec();
                offset = end;
                ChainedSignalState {
                    frame: value.clone(),
                    current: value,
                    previous_time_index: 0,
                    pending_record: None,
                    pending_frame_record: None,
                }
            })
            .collect::<Vec<_>>();
        let mut time_table = Vec::with_capacity(16);
        write_time_chain_update(&mut time_table, 0, start_time)?;
        Ok(Self {
            start_time,
            end_time: start_time,
            time_table,
            time_table_index: 0,
            first_file_section: false,
            value_changes: vec![Vec::new(); states.len()],
            signals: states,
            value_changes_bytes: 0,
            pack_cpu_seconds: 0.0,
            worker_cpu_seconds: Vec::new(),
            arena_to_packer_copied_bytes: 0,
        })
    }

    pub(crate) fn time_change(&mut self, new_time: u64) -> Result<u32> {
        match new_time.cmp(&self.end_time) {
            Ordering::Less => Err(FstWriteError::TimeDecrease(self.end_time, new_time)),
            Ordering::Equal => Ok(if self.time_table.is_empty() {
                FST_FRAME_TIME_INDEX
            } else {
                self.time_table_index
            }),
            Ordering::Greater => {
                if !self.time_table.is_empty() {
                    self.time_table_index =
                        self.time_table_index.checked_add(1).ok_or_else(|| {
                            FstWriteError::InvalidSignalChanges(
                                "section timestamp table exceeds u32".to_string(),
                            )
                        })?;
                }
                let previous = if self.time_table.is_empty() {
                    0
                } else {
                    self.end_time
                };
                write_time_chain_update(&mut self.time_table, previous, new_time)?;
                self.end_time = new_time;
                Ok(self.time_table_index)
            }
        }
    }

    pub(crate) fn current_time_index(&self) -> u32 {
        if self.time_table.is_empty() {
            FST_FRAME_TIME_INDEX
        } else {
            self.time_table_index
        }
    }

    pub(crate) fn apply_signal_chains(
        &mut self,
        first_by_signal: &[u32],
        changes: &[FstSignalChange],
        values: &[u8],
        pool: Option<&rayon::ThreadPool>,
    ) -> Result<()> {
        if first_by_signal.len() != self.signals.len() {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "{} signal chains supplied for {} signals",
                first_by_signal.len(),
                self.signals.len()
            )));
        }
        let max_time_index = self.current_time_index();
        let first_file_section = self.first_file_section;
        let stats = if let Some(pool) = pool {
            pool.install(|| {
                self.signals
                    .par_iter_mut()
                    .zip(self.value_changes.par_iter_mut())
                    .enumerate()
                    .map(|(signal_index, (state, stream))| {
                        apply_signal_chain(
                            signal_index,
                            state,
                            stream,
                            first_by_signal[signal_index],
                            changes,
                            values,
                            max_time_index,
                            first_file_section,
                        )
                    })
                    .collect::<Result<Vec<_>>>()
            })?
        } else {
            self.signals
                .iter_mut()
                .zip(self.value_changes.iter_mut())
                .enumerate()
                .map(|(signal_index, (state, stream))| {
                    apply_signal_chain(
                        signal_index,
                        state,
                        stream,
                        first_by_signal[signal_index],
                        changes,
                        values,
                        max_time_index,
                        first_file_section,
                    )
                })
                .collect::<Result<Vec<_>>>()?
        };
        let worker_count = pool.map_or(1, rayon::ThreadPool::current_num_threads);
        self.worker_cpu_seconds
            .resize(self.worker_cpu_seconds.len().max(worker_count), 0.0);
        for stat in stats {
            self.value_changes_bytes += stat.encoded_bytes;
            self.arena_to_packer_copied_bytes += stat.copied_bytes;
            self.pack_cpu_seconds += stat.cpu_seconds;
            self.worker_cpu_seconds[stat.worker_index] += stat.cpu_seconds;
        }
        Ok(())
    }

    pub(crate) fn apply_signal_records(
        &mut self,
        changes: &[FstSignalRecord],
        values: &[u8],
        pool: Option<&rayon::ThreadPool>,
    ) -> Result<()> {
        let max_time_index = self.current_time_index();
        let first_file_section = self.first_file_section;
        let worker_count = pool.map_or(1, rayon::ThreadPool::current_num_threads);
        let range_len = self.signals.len().max(1).div_ceil(worker_count);
        let stats = if let Some(pool) = pool {
            pool.install(|| {
                self.signals
                    .par_chunks_mut(range_len)
                    .zip(self.value_changes.par_chunks_mut(range_len))
                    .enumerate()
                    .map(|(range, (states, streams))| {
                        apply_signal_range(
                            range * range_len,
                            states,
                            streams,
                            changes,
                            values,
                            max_time_index,
                            first_file_section,
                        )
                    })
                    .collect::<Result<Vec<_>>>()
            })?
        } else {
            vec![apply_signal_range(
                0,
                &mut self.signals,
                &mut self.value_changes,
                changes,
                values,
                max_time_index,
                first_file_section,
            )?]
        };
        self.worker_cpu_seconds
            .resize(self.worker_cpu_seconds.len().max(worker_count), 0.0);
        for stat in stats {
            self.value_changes_bytes += stat.encoded_bytes;
            self.arena_to_packer_copied_bytes += stat.copied_bytes;
            self.pack_cpu_seconds += stat.cpu_seconds;
            self.worker_cpu_seconds[stat.worker_index] += stat.cpu_seconds;
        }
        Ok(())
    }

    pub(crate) fn apply_signal_fragments(
        &mut self,
        fragments: &[FstSignalFragment],
        fragment_slots: &[u32],
        values: &[u8],
        time_map: &[u32],
        time_map_is_affine: bool,
        pool: Option<&rayon::ThreadPool>,
    ) -> Result<()> {
        if fragment_slots.len() != self.signals.len() {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "{} fragment slots supplied for {} signals",
                fragment_slots.len(),
                self.signals.len()
            )));
        }
        let first_file_section = self.first_file_section;
        let worker_count = pool.map_or(1, rayon::ThreadPool::current_num_threads);
        let range_len = self.signals.len().max(1).div_ceil(worker_count);
        let stats = if let Some(pool) = pool {
            pool.install(|| {
                self.signals
                    .par_chunks_mut(range_len)
                    .zip(self.value_changes.par_chunks_mut(range_len))
                    .enumerate()
                    .map(|(range, (states, streams))| {
                        apply_fragment_range(
                            range * range_len,
                            states,
                            streams,
                            fragments,
                            fragment_slots,
                            values,
                            time_map,
                            time_map_is_affine,
                            first_file_section,
                        )
                    })
                    .collect::<Result<Vec<_>>>()
            })?
        } else {
            vec![apply_fragment_range(
                0,
                &mut self.signals,
                &mut self.value_changes,
                fragments,
                fragment_slots,
                values,
                time_map,
                time_map_is_affine,
                first_file_section,
            )?]
        };
        self.worker_cpu_seconds
            .resize(self.worker_cpu_seconds.len().max(worker_count), 0.0);
        for stat in stats {
            self.value_changes_bytes += stat.encoded_bytes;
            self.arena_to_packer_copied_bytes += stat.copied_bytes;
            self.pack_cpu_seconds += stat.cpu_seconds;
            self.worker_cpu_seconds[stat.worker_index] += stat.cpu_seconds;
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

    pub(crate) fn encode(
        &mut self,
        compression_pool: Option<&rayon::ThreadPool>,
    ) -> Result<(Vec<u8>, u64, f64, ChainedBufferStats)> {
        let mut frame =
            Vec::with_capacity(self.signals.iter().map(|state| state.frame.len()).sum());
        for state in &self.signals {
            frame.extend_from_slice(&state.frame);
        }
        let mut output = Cursor::new(Vec::with_capacity(self.size()));
        let compression_cpu_seconds = write_value_change_section(
            &mut output,
            self.start_time,
            self.end_time,
            &frame,
            &self.time_table,
            self.num_time_table_entries(),
            &self.value_changes,
            compression_pool,
        )?;
        let packer_to_compressor_bytes = self
            .value_changes
            .iter()
            .filter(|stream| stream.len() >= MIN_SIZE_TO_ATTEMPT_COMPRESSION)
            .map(Vec::len)
            .sum();
        Ok((
            output.into_inner(),
            self.end_time,
            compression_cpu_seconds,
            ChainedBufferStats {
                pack_cpu_seconds: self.pack_cpu_seconds,
                worker_cpu_seconds: std::mem::take(&mut self.worker_cpu_seconds),
                arena_to_packer_copied_bytes: self.arena_to_packer_copied_bytes,
                packer_to_compressor_bytes,
            },
        ))
    }

    pub(crate) fn size(&self) -> usize {
        self.time_table.len() + self.value_changes_bytes
    }
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

    pub(crate) fn flush(
        &mut self,
        output: &mut (impl Write + Seek),
        compression_pool: Option<&rayon::ThreadPool>,
    ) -> Result<(u64, f64)> {
        // A timestamp-aligned section may end before a later time change
        // (notably when the first timestamp contains one very wide value).
        // Ordinarily `time_change` snapshots first-step values into `frame`;
        // do it here as well when this section contains only that step.
        if self.first_buffer && self.time_table.is_empty() {
            self.frame.copy_from_slice(&self.values);
        }
        // write data
        let compression_cpu_seconds = write_value_change_section(
            output,
            self.start_time,
            self.end_time,
            &self.frame,
            &self.time_table,
            self.num_time_table_entries(),
            &self.value_changes,
            compression_pool,
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

        Ok((self.end_time, compression_cpu_seconds))
    }

    pub(crate) fn encode(
        &mut self,
        compression_pool: Option<&rayon::ThreadPool>,
    ) -> Result<(Vec<u8>, u64, f64)> {
        let mut output = Cursor::new(Vec::with_capacity(self.size()));
        let (end_time, compression_cpu_seconds) = self.flush(&mut output, compression_pool)?;
        Ok((output.into_inner(), end_time, compression_cpu_seconds))
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
