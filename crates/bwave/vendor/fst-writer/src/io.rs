// Copyright 2023 The Regents of the University of California
// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

use crate::FstWriteError::InvalidCharacter;
use crate::writer::{FST_FRAME_TIME_INDEX, FST_NO_CHANGE, FstDumpState, FstSignalChange};
use crate::profile::thread_cpu_seconds;
use crate::{
    FstInfo, FstScopeType, FstSignalId, FstSignalType, FstVarDirection, FstVarType, FstWriteError,
    Result,
};
use std::cell::RefCell;
use std::io::{Cursor, Seek, SeekFrom, Write};

use rayon::prelude::*;

#[inline]
pub(crate) fn write_variant_u64(output: &mut impl Write, mut value: u64) -> Result<usize> {
    // often, the value is small
    if value <= 0x7f {
        let byte = [value as u8; 1];
        output.write_all(&byte)?;
        return Ok(1);
    }

    let mut bytes = [0u8; 10];
    let mut len = 0usize;
    while value != 0 {
        let next_value = value >> 7;
        let mask: u8 = if next_value == 0 { 0 } else { 0x80 };
        bytes[len] = (value & 0x7f) as u8 | mask;
        len += 1;
        value = next_value;
    }
    output.write_all(&bytes[..len])?;
    Ok(len)
}

#[inline]
pub(crate) fn write_variant_i64(output: &mut impl Write, mut value: i64) -> Result<usize> {
    // often, the value is small
    if (-64..=63).contains(&value) {
        let byte = [value as u8 & 0x7f; 1];
        output.write_all(&byte)?;
        return Ok(1);
    }

    // calculate the number of bits we need to represent
    let bits = if value >= 0 {
        64 - value.leading_zeros() + 1
    } else {
        64 - value.leading_ones() + 1
    };
    let num_bytes = bits.div_ceil(7) as usize;

    let mut bytes = [0u8; 10];
    for ii in 0..num_bytes {
        let mark = if ii == num_bytes - 1 { 0 } else { 0x80 };
        bytes[ii] = (value & 0x7f) as u8 | mark;
        value >>= 7;
    }
    output.write_all(&bytes[..num_bytes])?;
    Ok(num_bytes)
}

#[inline]
pub(crate) fn write_u64(output: &mut impl Write, value: u64) -> Result<()> {
    let buf = value.to_be_bytes();
    output.write_all(&buf)?;
    Ok(())
}

fn write_u8(output: &mut impl Write, value: u8) -> Result<()> {
    let buf = value.to_be_bytes();
    output.write_all(&buf)?;
    Ok(())
}

#[inline]
fn write_i8(output: &mut impl Write, value: i8) -> Result<()> {
    let buf = value.to_be_bytes();
    output.write_all(&buf)?;
    Ok(())
}

fn write_c_str(output: &mut impl Write, value: impl AsRef<str>) -> Result<()> {
    let bytes = value.as_ref().as_bytes();
    output.write_all(bytes)?;
    write_u8(output, 0)?;
    Ok(())
}

#[inline]
fn write_c_str_fixed_length(output: &mut impl Write, value: &str, max_len: usize) -> Result<()> {
    let bytes = value.as_bytes();
    if bytes.len() >= max_len {
        return Err(FstWriteError::StringTooLong(max_len, value.to_string()));
    }
    output.write_all(bytes)?;
    let zeros = vec![0u8; max_len - bytes.len()];
    output.write_all(&zeros)?;
    Ok(())
}

#[inline]
fn write_f64(output: &mut impl Write, value: f64) -> Result<()> {
    // for f64, we have the option to use either LE or BE, we just need to be consistent
    let buf = value.to_le_bytes();
    output.write_all(&buf)?;
    Ok(())
}

const HEADER_LENGTH: u64 = 329;
const HEADER_VERSION_MAX_LEN: usize = 128;
const HEADER_DATE_MAX_LEN: usize = 119;
const DOUBLE_ENDIAN_TEST: f64 = std::f64::consts::E;

#[repr(u8)]
#[derive(Debug, PartialEq)]
enum BlockType {
    Header = 0,
    Geometry = 3,
    HierarchyLZ4 = 6,
    VcDataDynamicAlias2 = 8,
}

//////////////// Header
const HEADER_POS: u64 = 0;

/// Writes the user supplied meta-data to the header. We will come back to the header later to
/// fill in other data.
pub(crate) fn write_header_meta_data(
    output: &mut (impl Write + Seek),
    info: &FstInfo,
) -> Result<()> {
    debug_assert_eq!(
        output.stream_position().unwrap(),
        HEADER_POS,
        "We expect the header to be written at position {HEADER_POS}"
    );
    write_u8(output, BlockType::Header as u8)?;
    write_u64(output, HEADER_LENGTH)?;
    write_u64(output, 0)?; // start time is always zero
    write_u64(output, 0)?; // dummy end time
    write_f64(output, DOUBLE_ENDIAN_TEST)?;
    write_u64(output, 0)?; // memory used by writer is always zero, we do not compute this
    write_u64(output, 0)?; // dummy scope count
    write_u64(output, 0)?; // dummy var count
    write_u64(output, 0)?; // dummy num signals
    write_u64(output, 0)?; // dummy num vc sections
    write_i8(output, info.timescale_exponent)?;
    write_c_str_fixed_length(output, &info.version, HEADER_VERSION_MAX_LEN)?;
    write_c_str_fixed_length(output, &info.date, HEADER_DATE_MAX_LEN)?;
    write_u8(output, info.file_type as u8)?;
    write_u64(output, info.start_time)?; // offset?
    Ok(())
}

pub(crate) struct HeaderFinishInfo {
    pub(crate) end_time: u64,
    pub(crate) scope_count: u64,
    pub(crate) var_count: u64,
    pub(crate) num_signals: u64,
    pub(crate) num_value_change_sections: u64,
}

pub(crate) fn update_header(
    output: &mut (impl Write + Seek),
    info: &HeaderFinishInfo,
) -> Result<()> {
    // go to start of header + skip block type, length and start time
    output.seek(SeekFrom::Start(HEADER_POS + 1 + 2 * 8))?;
    write_u64(output, info.end_time)?;
    // skip endian test + writer memory
    output.seek(SeekFrom::Current(2 * 8))?;
    write_u64(output, info.scope_count)?;
    write_u64(output, info.var_count)?;
    write_u64(output, info.num_signals)?;
    write_u64(output, info.num_value_change_sections)?;
    Ok(())
}

//////////////// Hierarchy

const HIERARCHY_TPE_VCD_SCOPE: u8 = 254;
const HIERARCHY_TPE_VCD_UP_SCOPE: u8 = 255;
// const HIERARCHY_TPE_VCD_ATTRIBUTE_BEGIN: u8 = 252;
// const HIERARCHY_TPE_VCD_ATTRIBUTE_END: u8 = 253;
const HIERARCHY_NAME_MAX_SIZE: usize = 512;
// const HIERARCHY_ATTRIBUTE_MAX_SIZE: usize = 65536 + 4096;

pub(crate) fn write_hierarchy_bytes(output: &mut (impl Write + Seek), bytes: &[u8]) -> Result<()> {
    write_u8(output, BlockType::HierarchyLZ4 as u8)?;
    // remember start to fix the section length afterward
    let start = output.stream_position()?;
    write_u64(output, 0)?; // dummy section length
    let uncompressed_length = bytes.len() as u64;
    write_u64(output, uncompressed_length)?;

    // we only support single LZ4 compression
    let out2 = {
        let compressed = lz4_flex::compress(bytes);
        output.write_all(&compressed)?;
        output
    };

    // fix section length
    let end = out2.stream_position()?;
    out2.seek(SeekFrom::Start(start))?;
    write_u64(out2, end - start)?;
    out2.seek(SeekFrom::Start(end))?;
    Ok(())
}

pub(crate) fn write_hierarchy_scope(
    output: &mut impl Write,
    name: impl AsRef<str>,
    component: impl AsRef<str>,
    tpe: FstScopeType,
) -> Result<()> {
    write_u8(output, HIERARCHY_TPE_VCD_SCOPE)?;
    write_u8(output, tpe as u8)?;
    debug_assert!(name.as_ref().len() <= HIERARCHY_NAME_MAX_SIZE);
    write_c_str(output, name)?;
    debug_assert!(component.as_ref().len() <= HIERARCHY_NAME_MAX_SIZE);
    write_c_str(output, component)?;
    Ok(())
}

pub(crate) fn write_hierarchy_up_scope(output: &mut impl Write) -> Result<()> {
    write_u8(output, HIERARCHY_TPE_VCD_UP_SCOPE)
}

pub(crate) fn write_hierarchy_var(
    output: &mut impl Write,
    tpe: FstVarType,
    direction: FstVarDirection,
    name: impl AsRef<str>,
    signal_tpe: FstSignalType,
    alias: Option<FstSignalId>,
) -> Result<()> {
    write_u8(output, tpe as u8)?;
    write_u8(output, direction as u8)?;
    debug_assert!(name.as_ref().len() <= HIERARCHY_NAME_MAX_SIZE);
    write_c_str(output, name)?;
    let length = signal_tpe.len();
    let raw_length = if tpe == FstVarType::Port {
        3 * length + 2
    } else {
        length
    };
    write_variant_u64(output, raw_length as u64)?;
    write_variant_u64(
        output,
        alias.map(|id| id.to_index()).unwrap_or_default() as u64,
    )?;
    Ok(())
}

//////////////// Geometry

pub(crate) fn write_geometry(
    output: &mut (impl Write + Seek),
    signals: &[FstSignalType],
) -> Result<()> {
    write_u8(output, BlockType::Geometry as u8)?;
    // remember start to fix the section header
    let start = output.stream_position()?;
    write_u64(output, 0)?; // dummy section length
    write_u64(output, 0)?; // dummy uncompressed section length
    let max_handle = signals.len() as u64;
    write_u64(output, max_handle)?;

    for signal in signals.iter() {
        write_variant_u64(output, signal.to_file_format() as u64)?;
    }

    // remember the end
    let end = output.stream_position()?;
    // fix section header
    let section_len = end - start;
    output.seek(SeekFrom::Start(start))?;
    write_u64(output, section_len)?; // section length
    write_u64(output, section_len - 3 * 8)?; // uncompressed section _content_ length
    // return cursor back to end
    output.seek(SeekFrom::Start(end))?;

    Ok(())
}

//////////////// Value Change Data

#[inline]
pub(crate) fn write_one_bit_signal(
    output: &mut impl Write,
    time_delta: u64,
    value: u8,
) -> Result<()> {
    let vli = match value {
        b'0' | b'1' => {
            let bit = value - b'0';
            // 2-bits are used to encode the signal value
            let shift_count = 2;
            (time_delta << shift_count) | ((bit as u64) << 1)
        }
        _ => {
            if let Some(encoding) = encode_9_value(value) {
                // 4-bits are used to encode the signal value
                let shift_count = 4;
                (time_delta << shift_count) | ((encoding as u64) << 1) | 1
            } else {
                return Err(InvalidCharacter(value as char));
            }
        }
    };
    write_variant_u64(output, vli)?;
    Ok(())
}

#[inline]
pub(crate) fn write_multi_bit_signal(
    output: &mut impl Write,
    time_delta: u64,
    values: &[u8],
) -> Result<()> {
    let is_digital = is_digital(values);
    // write time delta
    write_variant_u64(output, (time_delta << 1) | (!is_digital as u64))?;
    // digital signals get a special encoding
    if is_digital {
        let mut wip_byte = 0u8;
        for (ii, value) in values.iter().enumerate() {
            let bit = *value - b'0';
            let bit_id = 7 - (ii & 0x7);
            wip_byte |= bit << bit_id;
            if bit_id == 0 {
                write_u8(output, wip_byte)?;
                wip_byte = 0;
            }
        }
        if values.len() % 8 != 0 {
            write_u8(output, wip_byte)?;
        }
    } else {
        output.write_all(values)?;
    }
    Ok(())
}

#[inline]
pub(crate) fn write_packed_binary_signal(
    output: &mut impl Write,
    time_delta: u64,
    values: &[u8],
) -> Result<()> {
    write_variant_u64(output, time_delta << 1)?;
    output.write_all(values)?;
    Ok(())
}

#[allow(dead_code)]
#[inline]
pub(crate) fn write_real_signal(
    output: &mut impl Write,
    time_delta: u64,
    value: f64,
) -> Result<()> {
    // write time delta, bit 0 should always be zero, otherwise we are triggering the "rare packed case"
    write_variant_u64(output, time_delta << 1)?;
    output.write_all(value.to_le_bytes().as_slice())?;
    Ok(())
}

#[inline]
fn is_digital(values: &[u8]) -> bool {
    values.iter().all(|v| matches!(*v, b'0' | b'1'))
}

#[inline]
fn encode_9_value(value: u8) -> Option<u8> {
    match value {
        b'x' | b'X' => Some(0),
        b'z' | b'Z' => Some(1),
        b'h' | b'H' => Some(2),
        b'u' | b'U' => Some(3),
        b'w' | b'W' => Some(4),
        b'l' | b'L' => Some(5),
        b'-' => Some(6),
        b'?' => Some(7),
        _ => None,
    }
}

#[inline]
pub(crate) fn write_time_chain_update(
    output: &mut impl Write,
    prev_time: u64,
    current_time: u64,
) -> Result<()> {
    debug_assert!(current_time >= prev_time);
    let delta = current_time - prev_time;
    write_variant_u64(output, delta)?;
    Ok(())
}

const VALUE_CHANGE_PACK_TYPE_ZLIB: u8 = b'Z';

#[inline]
fn flush_zeros(output: &mut impl Write, zeros: &mut u32) -> Result<()> {
    if *zeros > 0 {
        // shifted by one because bit0 indicates whether we are dealing with a zero or a real offset
        let value = *zeros << 1;
        write_variant_u64(output, value as u64)?;
        *zeros = 0;
    }
    debug_assert_eq!(*zeros, 0);
    Ok(())
}

/// For any signal change streams smaller than this size, skip compression.
pub(crate) const MIN_SIZE_TO_ATTEMPT_COMPRESSION: usize = 32;

enum PackedSignal<'a> {
    Empty,
    Raw(&'a [u8]),
    Zlib {
        uncompressed_len: usize,
        bytes: Vec<u8>,
    },
}

enum OwnedPackedSignal {
    Empty(Vec<u8>),
    Raw(Vec<u8>),
    Zlib {
        uncompressed_len: usize,
        bytes: Vec<u8>,
    },
}

thread_local! {
    static SIGNAL_PACK_BUFFER: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

fn take_signal_pack_buffer() -> Vec<u8> {
    SIGNAL_PACK_BUFFER.with(|buffer| std::mem::take(&mut *buffer.borrow_mut()))
}

fn recycle_signal_pack_buffer(mut value: Vec<u8>) {
    value.clear();
    SIGNAL_PACK_BUFFER.with(|buffer| {
        let mut recycled = buffer.borrow_mut();
        if value.capacity() > recycled.capacity() {
            *recycled = value;
        }
    });
}

struct ChainedSignalResult {
    signal: OwnedPackedSignal,
    frame_record: Option<u32>,
    pack_cpu_seconds: f64,
    compression_cpu_seconds: f64,
    packer_input_bytes: usize,
    worker_index: usize,
    recycled_capacity_bytes: usize,
    newly_allocated_capacity_bytes: usize,
}

pub(crate) struct ChainedWriteStats {
    pub(crate) pack_cpu_seconds: f64,
    pub(crate) compression_cpu_seconds: f64,
    pub(crate) packer_input_bytes: usize,
    pub(crate) worker_cpu_seconds: Vec<f64>,
    pub(crate) recycled_capacity_bytes: usize,
    pub(crate) newly_allocated_capacity_bytes: usize,
}

struct PackedSignalResult<'a> {
    signal: PackedSignal<'a>,
    compression_cpu_seconds: f64,
}

fn pack_signal(data: &[u8]) -> PackedSignalResult<'_> {
    if data.is_empty() {
        return PackedSignalResult {
            signal: PackedSignal::Empty,
            compression_cpu_seconds: 0.0,
        };
    }
    if data.len() < MIN_SIZE_TO_ATTEMPT_COMPRESSION {
        return PackedSignalResult {
            signal: PackedSignal::Raw(data),
            compression_cpu_seconds: 0.0,
        };
    }
    let cpu_started = thread_cpu_seconds();
    let compressed = miniz_oxide::deflate::compress_to_vec_zlib(data, VALUE_ZLIB_LEVEL);
    let compression_cpu_seconds = thread_cpu_seconds() - cpu_started;
    let signal = if compressed.len() < data.len() {
        PackedSignal::Zlib {
            uncompressed_len: data.len(),
            bytes: compressed,
        }
    } else {
        PackedSignal::Raw(data)
    };
    PackedSignalResult {
        signal,
        compression_cpu_seconds,
    }
}

fn pack_owned_signal(data: Vec<u8>) -> (OwnedPackedSignal, f64) {
    if data.is_empty() {
        return (OwnedPackedSignal::Empty(data), 0.0);
    }
    if data.len() < MIN_SIZE_TO_ATTEMPT_COMPRESSION {
        return (OwnedPackedSignal::Raw(data), 0.0);
    }
    let cpu_started = thread_cpu_seconds();
    let compressed = miniz_oxide::deflate::compress_to_vec_zlib(&data, VALUE_ZLIB_LEVEL);
    let compression_cpu_seconds = thread_cpu_seconds() - cpu_started;
    if compressed.len() < data.len() {
        let uncompressed_len = data.len();
        recycle_signal_pack_buffer(data);
        (
            OwnedPackedSignal::Zlib {
                uncompressed_len,
                bytes: compressed,
            },
            compression_cpu_seconds,
        )
    } else {
        (OwnedPackedSignal::Raw(data), compression_cpu_seconds)
    }
}

fn write_packed_signal(
    output: &mut (impl Write + Seek),
    signal_offsets: &mut impl Write,
    memory_required: &mut u64,
    zero_count: &mut u32,
    prev_offset: &mut u64,
    signal: PackedSignal<'_>,
) -> Result<()> {
    match signal {
        PackedSignal::Empty => *zero_count += 1,
        PackedSignal::Raw(data) => {
            flush_zeros(signal_offsets, zero_count)?;
            let start = output.stream_position()?;
            *memory_required += data.len() as u64;
            write_variant_u64(output, 0)?;
            output.write_all(data)?;
            let offset_delta = (start - *prev_offset) as i64;
            write_variant_i64(signal_offsets, (offset_delta << 1) | 1)?;
            *prev_offset = start;
        }
        PackedSignal::Zlib {
            uncompressed_len,
            bytes,
        } => {
            flush_zeros(signal_offsets, zero_count)?;
            let start = output.stream_position()?;
            *memory_required += uncompressed_len as u64;
            write_variant_u64(output, uncompressed_len as u64)?;
            output.write_all(&bytes)?;
            let offset_delta = (start - *prev_offset) as i64;
            write_variant_i64(signal_offsets, (offset_delta << 1) | 1)?;
            *prev_offset = start;
        }
    }
    Ok(())
}

fn write_owned_packed_signal(
    output: &mut (impl Write + Seek),
    signal_offsets: &mut impl Write,
    memory_required: &mut u64,
    zero_count: &mut u32,
    prev_offset: &mut u64,
    signal: OwnedPackedSignal,
) -> Result<()> {
    match signal {
        OwnedPackedSignal::Empty(data) => {
            *zero_count += 1;
            recycle_signal_pack_buffer(data);
            Ok(())
        }
        OwnedPackedSignal::Raw(data) => {
            let result = write_packed_signal(
                output,
                signal_offsets,
                memory_required,
                zero_count,
                prev_offset,
                PackedSignal::Raw(&data),
            );
            recycle_signal_pack_buffer(data);
            result
        }
        OwnedPackedSignal::Zlib {
            uncompressed_len,
            bytes,
        } => write_packed_signal(
            output,
            signal_offsets,
            memory_required,
            zero_count,
            prev_offset,
            PackedSignal::Zlib {
                uncompressed_len,
                bytes,
            },
        ),
    }
}

fn write_value_changes(
    output: &mut (impl Write + Seek),
    signal_data: &[Vec<u8>],
    compression_pool: Option<&rayon::ThreadPool>,
    signal_offsets: &mut impl Write,
    memory_required: &mut u64,
) -> Result<f64> {
    write_variant_u64(output, signal_data.len() as u64)?;
    // Zlib gives independent parallel sections enough local compression to
    // stay close to a long serial section. FST readers treat every pack marker
    // other than `4` (LZ4) and `F` (FastLZ) as zlib.
    write_u8(output, VALUE_CHANGE_PACK_TYPE_ZLIB)?;

    let mut zero_count = 0;
    let mut prev_offset = output.stream_position()? - 1;
    let mut compression_cpu_seconds = 0.0;
    if let Some(pool) = compression_pool {
        let packed: Vec<PackedSignalResult<'_>> = pool.install(|| {
            signal_data
                .par_iter()
                .map(|data| pack_signal(data))
                .collect()
        });
        for result in packed {
            compression_cpu_seconds += result.compression_cpu_seconds;
            write_packed_signal(
                output,
                signal_offsets,
                memory_required,
                &mut zero_count,
                &mut prev_offset,
                result.signal,
            )?;
        }
    } else {
        for data in signal_data {
            let result = pack_signal(data);
            compression_cpu_seconds += result.compression_cpu_seconds;
            write_packed_signal(
                output,
                signal_offsets,
                memory_required,
                &mut zero_count,
                &mut prev_offset,
                result.signal,
            )?;
        }
    }
    flush_zeros(signal_offsets, &mut zero_count)?;
    Ok(compression_cpu_seconds)
}

fn write_frame(output: &mut impl Write, frame: &[u8], num_signals: usize) -> Result<()> {
    let compressed = miniz_oxide::deflate::compress_to_vec_zlib(frame, ZLIB_LEVEL);
    let stored = if compressed.len() < frame.len() {
        compressed.as_slice()
    } else {
        frame
    };
    write_variant_u64(output, frame.len() as u64)?;
    write_variant_u64(output, stored.len() as u64)?;
    write_variant_u64(output, num_signals as u64)?;
    output.write_all(stored)?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn write_value_change_section(
    output: &mut (impl Write + Seek),
    start_time: u64,
    end_time: u64,
    frame: &[u8],
    time_table: &[u8],
    time_table_entries: u64,
    signal_data: &[Vec<u8>],
    compression_pool: Option<&rayon::ThreadPool>,
) -> Result<f64> {
    let num_signals = signal_data.len();
    // section header
    write_u8(output, BlockType::VcDataDynamicAlias2 as u8)?;
    // remember start to fix the section header
    let start = output.stream_position()?;
    write_u64(output, 0)?; // dummy section length
    write_u64(output, start_time)?;
    write_u64(output, end_time)?;
    let mut memory_required = 0;
    write_u64(output, memory_required)?;

    // frame, i.e., the initial values
    write_frame(output, frame, num_signals)?;

    // value change data
    let mut signal_offsets = vec![];
    let compression_cpu_seconds = write_value_changes(
        output,
        signal_data,
        compression_pool,
        &mut signal_offsets,
        &mut memory_required,
    )?;

    // offset table
    output.write_all(&signal_offsets)?;
    write_u64(output, signal_offsets.len() as u64)?;

    // time table at the end
    write_time_table(output, time_table, time_table_entries)?;

    // fix section length + memory requirement
    let end = output.stream_position()?;
    let section_len = end - start;
    output.seek(SeekFrom::Start(start))?;
    write_u64(output, section_len)?;
    output.seek(SeekFrom::Current(2 * 8))?;
    // the memory required for traversal is just the uncompressed length of all signals summed up
    write_u64(output, memory_required)?;
    output.seek(SeekFrom::Start(end))?;
    Ok(compression_cpu_seconds)
}

enum ChangeValue<'a> {
    Inline(u8),
    Arena(&'a [u8]),
}

impl ChangeValue<'_> {
    fn equals(&self, other: &Self) -> bool {
        match (self, other) {
            (Self::Inline(left), Self::Inline(right)) => left == right,
            (Self::Inline(left), Self::Arena(right)) | (Self::Arena(right), Self::Inline(left)) => {
                *right == std::slice::from_ref(left)
            }
            (Self::Arena(left), Self::Arena(right)) => left == right,
        }
    }

    fn copy_to(&self, output: &mut [u8]) {
        match self {
            Self::Inline(value) => output.copy_from_slice(std::slice::from_ref(value)),
            Self::Arena(value) => output.copy_from_slice(value),
        }
    }
}

fn change_value<'a>(
    change: &FstSignalChange,
    signal_len: usize,
    values: &'a [u8],
) -> Result<ChangeValue<'a>> {
    if change.is_inline() {
        if signal_len != 1 {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "inline value used for {}-byte signal {}",
                signal_len,
                change.signal()
            )));
        }
        return Ok(ChangeValue::Inline(change.inline_value()));
    }
    let start = change.value_offset() as usize;
    let end = start.checked_add(signal_len).ok_or_else(|| {
        FstWriteError::InvalidSignalChanges("value range exceeds usize".to_string())
    })?;
    let value = values.get(start..end).ok_or_else(|| {
        FstWriteError::InvalidSignalChanges(format!(
            "value range {start}..{end} is outside the arena"
        ))
    })?;
    Ok(ChangeValue::Arena(value))
}

#[allow(clippy::too_many_arguments)]
fn pack_signal_chain(
    signal_index: usize,
    signal_len: usize,
    incoming_value: &[u8],
    first_record: u32,
    changes: &[FstSignalChange],
    values: &[u8],
    time_point_count: usize,
    first_file_section: bool,
    incoming_dump_enabled: bool,
) -> Result<ChainedSignalResult> {
    let cpu_started = thread_cpu_seconds();
    let mut stream = take_signal_pack_buffer();
    let recycled_capacity_bytes = stream.capacity();
    stream.clear();
    let mut current_record = None;
    let mut frame_record = None;
    let mut previous_time_index = 0u32;
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
        if change.signal() as usize != signal_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "record {record_index} belongs to signal {}, expected {signal_index}",
                change.signal()
            )));
        }
        record_index = change.next();
        let enabled = match change.dump_state() {
            FstDumpState::Prefix => incoming_dump_enabled,
            FstDumpState::Enabled => true,
            FstDumpState::Suppressed => false,
        };
        if !enabled {
            continue;
        }
        let next_value = change_value(change, signal_len, values)?;
        if change.time_index() == FST_FRAME_TIME_INDEX {
            if !first_file_section {
                return Err(FstWriteError::InvalidSignalChanges(format!(
                    "frame-time change in noninitial section for signal {signal_index}"
                )));
            }
            current_record = Some(this_record);
            frame_record = current_record;
            continue;
        }
        if change.time_index() as usize >= time_point_count {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index {} for signal {signal_index} is out of bounds",
                change.time_index()
            )));
        }
        if change.time_index() < previous_time_index {
            return Err(FstWriteError::InvalidSignalChanges(format!(
                "time index decreased in signal {signal_index}"
            )));
        }
        let current_value = match current_record {
            Some(index) => change_value(&changes[index as usize], signal_len, values)?,
            None => ChangeValue::Arena(incoming_value),
        };
        if current_value.equals(&next_value) {
            continue;
        }
        let delta = u64::from(change.time_index() - previous_time_index);
        if signal_len == 1 {
            let value = match next_value {
                ChangeValue::Inline(value) => value,
                ChangeValue::Arena(value) => value[0],
            };
            write_one_bit_signal(&mut stream, delta, value)?;
        } else {
            let ChangeValue::Arena(value) = next_value else {
                unreachable!("inline values were rejected for wide signals")
            };
            write_multi_bit_signal(&mut stream, delta, value)?;
        }
        previous_time_index = change.time_index();
        current_record = Some(this_record);
    }
    let pack_cpu_seconds = thread_cpu_seconds() - cpu_started;
    let newly_allocated_capacity_bytes = stream.capacity().saturating_sub(recycled_capacity_bytes);
    let packer_input_bytes = if stream.len() >= MIN_SIZE_TO_ATTEMPT_COMPRESSION {
        stream.len()
    } else {
        0
    };
    let (signal, compression_cpu_seconds) = pack_owned_signal(stream);
    Ok(ChainedSignalResult {
        signal,
        frame_record,
        pack_cpu_seconds,
        compression_cpu_seconds,
        packer_input_bytes,
        worker_index: rayon::current_thread_index().unwrap_or(0),
        recycled_capacity_bytes,
        newly_allocated_capacity_bytes,
    })
}

fn encode_time_points(time_points: &[u64]) -> Result<Vec<u8>> {
    let mut encoded = Vec::with_capacity(time_points.len().saturating_mul(2));
    let mut previous = 0u64;
    for &time in time_points {
        if time < previous {
            return Err(FstWriteError::TimeDecrease(previous, time));
        }
        write_time_chain_update(&mut encoded, previous, time)?;
        previous = time;
    }
    Ok(encoded)
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn write_chained_value_change_section(
    signals: &[FstSignalType],
    incoming_frame: &[u8],
    start_time: u64,
    end_time: u64,
    first_file_section: bool,
    time_points: &[u64],
    first_by_signal: &[u32],
    changes: &[FstSignalChange],
    values: &[u8],
    incoming_dump_enabled: bool,
    pool: Option<&rayon::ThreadPool>,
) -> Result<(Vec<u8>, ChainedWriteStats)> {
    if first_by_signal.len() != signals.len() {
        return Err(FstWriteError::InvalidSignalChanges(format!(
            "{} signal chains supplied for {} signals",
            first_by_signal.len(),
            signals.len()
        )));
    }
    let mut offsets = Vec::with_capacity(signals.len());
    let mut frame_len = 0usize;
    for signal in signals {
        offsets.push(frame_len);
        frame_len = frame_len
            .checked_add(signal.len() as usize)
            .ok_or_else(|| FstWriteError::InvalidFrameLength {
                expected: usize::MAX,
                actual: incoming_frame.len(),
            })?;
    }
    if incoming_frame.len() != frame_len {
        return Err(FstWriteError::InvalidFrameLength {
            expected: frame_len,
            actual: incoming_frame.len(),
        });
    }
    let pack_one = |signal_index: usize| {
        let start = offsets[signal_index];
        let len = signals[signal_index].len() as usize;
        pack_signal_chain(
            signal_index,
            len,
            &incoming_frame[start..start + len],
            first_by_signal[signal_index],
            changes,
            values,
            time_points.len(),
            first_file_section,
            incoming_dump_enabled,
        )
    };
    let packed: Vec<ChainedSignalResult> = if let Some(pool) = pool {
        pool.install(|| {
            (0..signals.len())
                .into_par_iter()
                .map(pack_one)
                .collect::<Result<Vec<_>>>()
        })?
    } else {
        (0..signals.len())
            .map(pack_one)
            .collect::<Result<Vec<_>>>()?
    };

    let mut frame = incoming_frame.to_vec();
    for (signal_index, result) in packed.iter().enumerate() {
        if let Some(record_index) = result.frame_record {
            let start = offsets[signal_index];
            let len = signals[signal_index].len() as usize;
            change_value(&changes[record_index as usize], len, values)?
                .copy_to(&mut frame[start..start + len]);
        }
    }
    let time_table = encode_time_points(time_points)?;
    let mut output = Cursor::new(Vec::new());
    write_u8(&mut output, BlockType::VcDataDynamicAlias2 as u8)?;
    let section_start = output.stream_position()?;
    write_u64(&mut output, 0)?;
    write_u64(&mut output, start_time)?;
    write_u64(&mut output, end_time)?;
    write_u64(&mut output, 0)?;
    write_frame(&mut output, &frame, signals.len())?;
    write_variant_u64(&mut output, signals.len() as u64)?;
    write_u8(&mut output, VALUE_CHANGE_PACK_TYPE_ZLIB)?;

    let mut signal_offsets = Vec::new();
    let mut memory_required = 0u64;
    let mut zero_count = 0u32;
    let mut previous_offset = output.stream_position()? - 1;
    let mut pack_cpu_seconds = 0.0;
    let mut compression_cpu_seconds = 0.0;
    let mut packer_input_bytes = 0usize;
    let worker_count = pool.map_or(1, rayon::ThreadPool::current_num_threads);
    let mut worker_cpu_seconds = vec![0.0; worker_count];
    let mut recycled_capacity_bytes = 0usize;
    let mut newly_allocated_capacity_bytes = 0usize;
    for result in packed {
        pack_cpu_seconds += result.pack_cpu_seconds;
        compression_cpu_seconds += result.compression_cpu_seconds;
        packer_input_bytes += result.packer_input_bytes;
        worker_cpu_seconds[result.worker_index] +=
            result.pack_cpu_seconds + result.compression_cpu_seconds;
        recycled_capacity_bytes += result.recycled_capacity_bytes;
        newly_allocated_capacity_bytes += result.newly_allocated_capacity_bytes;
        write_owned_packed_signal(
            &mut output,
            &mut signal_offsets,
            &mut memory_required,
            &mut zero_count,
            &mut previous_offset,
            result.signal,
        )?;
    }
    flush_zeros(&mut signal_offsets, &mut zero_count)?;
    output.write_all(&signal_offsets)?;
    write_u64(&mut output, signal_offsets.len() as u64)?;
    write_time_table(&mut output, &time_table, time_points.len() as u64)?;

    let section_end = output.stream_position()?;
    output.seek(SeekFrom::Start(section_start))?;
    write_u64(&mut output, section_end - section_start)?;
    output.seek(SeekFrom::Current(2 * 8))?;
    write_u64(&mut output, memory_required)?;
    output.seek(SeekFrom::Start(section_end))?;
    Ok((
        output.into_inner(),
        ChainedWriteStats {
            pack_cpu_seconds,
            compression_cpu_seconds,
            packer_input_bytes,
            worker_cpu_seconds,
            recycled_capacity_bytes,
            newly_allocated_capacity_bytes,
        },
    ))
}

/// by unscientific experiment, we observed that this level might be good enough :)
const ZLIB_LEVEL: u8 = 3;
const VALUE_ZLIB_LEVEL: u8 = 1;

fn write_time_table(
    output: &mut (impl Write + Seek),
    time_table: &[u8],
    time_table_entries: u64,
) -> Result<()> {
    // zlib compress
    let compressed = miniz_oxide::deflate::compress_to_vec_zlib(time_table, ZLIB_LEVEL);

    // is compression worth it?
    if compressed.len() > time_table.len() {
        // it is more space efficient to stick with the uncompressed version
        output.write_all(time_table)?;
        write_u64(output, time_table.len() as u64)?;
        write_u64(output, time_table.len() as u64)?;
    } else {
        output.write_all(compressed.as_slice())?;
        write_u64(output, time_table.len() as u64)?;
        write_u64(output, compressed.len() as u64)?;
    }
    write_u64(output, time_table_entries)?;
    Ok(())
}
