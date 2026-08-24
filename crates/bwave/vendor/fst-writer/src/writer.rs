// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

use crate::buffer::{ChainedSignalBuffer, SignalBuffer};
use crate::io::{
    HeaderFinishInfo, update_header, write_chained_value_change_section, write_geometry,
    write_header_meta_data, write_hierarchy_bytes, write_hierarchy_scope, write_hierarchy_up_scope,
    write_hierarchy_var,
};
use crate::{
    FstInfo, FstScopeType, FstSignalId, FstSignalType, FstVarDirection, FstVarType, Result,
};
use std::sync::Arc;

pub const FST_NO_CHANGE: u32 = u32::MAX;
pub const FST_FRAME_TIME_INDEX: u32 = u32::MAX;
const INLINE_VALUE: u32 = 1 << 31;
const PACKED_BINARY_VALUE: u32 = 1 << 30;
const VALUE_OFFSET_MASK: u32 = PACKED_BINARY_VALUE - 1;
const SIGNAL_MASK: u32 = (1 << 30) - 1;

/// Whether a parsed value change is unconditional, controlled by the dump
/// state at the section boundary, or suppressed by a local `$dumpoff`.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[repr(u32)]
pub enum FstDumpState {
    Prefix = 0,
    Enabled = 1,
    Suppressed = 2,
}

/// Compact chronological change record consumed by the incremental section
/// encoder. The dump classification occupies the high two bits of
/// `signal_and_state`.
#[derive(Clone, Copy, Debug)]
#[repr(C)]
pub struct FstSignalRecord {
    signal_and_state: u32,
    time_index: u32,
    payload: u32,
}

const _: [(); 12] = [(); std::mem::size_of::<FstSignalRecord>()];

impl FstSignalRecord {
    #[inline]
    pub fn inline(signal: u32, time_index: u32, value: u8, state: FstDumpState) -> Result<Self> {
        Self::new(signal, time_index, INLINE_VALUE | u32::from(value), state)
    }

    #[inline]
    pub fn arena(
        signal: u32,
        time_index: u32,
        value_offset: u32,
        state: FstDumpState,
    ) -> Result<Self> {
        if value_offset > VALUE_OFFSET_MASK {
            return Err(crate::FstWriteError::InvalidSignalChanges(
                "value arena exceeds 1 GiB".to_string(),
            ));
        }
        Self::new(signal, time_index, value_offset, state)
    }

    #[inline]
    pub fn packed_binary(
        signal: u32,
        time_index: u32,
        value_offset: u32,
        state: FstDumpState,
    ) -> Result<Self> {
        if value_offset > VALUE_OFFSET_MASK {
            return Err(crate::FstWriteError::InvalidSignalChanges(
                "value arena exceeds 1 GiB".to_string(),
            ));
        }
        Self::new(
            signal,
            time_index,
            PACKED_BINARY_VALUE | value_offset,
            state,
        )
    }

    #[inline]
    fn new(signal: u32, time_index: u32, payload: u32, state: FstDumpState) -> Result<Self> {
        if signal > SIGNAL_MASK {
            return Err(crate::FstWriteError::InvalidSignalChanges(
                "signal index exceeds 30 bits".to_string(),
            ));
        }
        Ok(Self {
            signal_and_state: signal | (state as u32) << 30,
            time_index,
            payload,
        })
    }

    #[inline]
    pub fn signal(&self) -> u32 {
        self.signal_and_state & SIGNAL_MASK
    }

    #[inline]
    pub fn dump_state(&self) -> FstDumpState {
        match self.signal_and_state >> 30 {
            0 => FstDumpState::Prefix,
            1 => FstDumpState::Enabled,
            2 => FstDumpState::Suppressed,
            _ => unreachable!("validated dump-state encoding"),
        }
    }

    #[inline]
    pub fn set_dump_state(&mut self, state: FstDumpState) {
        self.signal_and_state = self.signal() | (state as u32) << 30;
    }

    #[inline]
    pub fn time_index(&self) -> u32 {
        self.time_index
    }

    #[inline]
    pub fn set_time_index(&mut self, time_index: u32) {
        self.time_index = time_index;
    }

    #[inline]
    pub fn is_inline(&self) -> bool {
        self.payload & INLINE_VALUE != 0
    }

    #[inline]
    pub fn is_packed_binary(&self) -> bool {
        self.payload & PACKED_BINARY_VALUE != 0 && !self.is_inline()
    }

    #[inline]
    pub fn inline_value(&self) -> u8 {
        self.payload as u8
    }

    #[inline]
    pub fn value_offset(&self) -> u32 {
        self.payload & VALUE_OFFSET_MASK
    }

    pub fn rebase_value_offset(&mut self, base: u32) -> Result<()> {
        if self.is_inline() {
            return Ok(());
        }
        let offset = self.value_offset().checked_add(base).ok_or_else(|| {
            crate::FstWriteError::InvalidSignalChanges(
                "rebased value offset exceeds u32".to_string(),
            )
        })?;
        if offset > VALUE_OFFSET_MASK {
            return Err(crate::FstWriteError::InvalidSignalChanges(
                "rebased value arena exceeds 1 GiB".to_string(),
            ));
        }
        let format = self.payload & PACKED_BINARY_VALUE;
        self.payload = format | offset;
        Ok(())
    }
}

/// Signal-linked change record consumed by the legacy bulk section encoder.
#[derive(Clone, Copy, Debug)]
#[repr(C)]
pub struct FstSignalChange {
    record: FstSignalRecord,
    next: u32,
}

impl FstSignalChange {
    pub fn inline(signal: u32, time_index: u32, value: u8, state: FstDumpState) -> Result<Self> {
        Self::new(signal, time_index, INLINE_VALUE | u32::from(value), state)
    }

    pub fn arena(
        signal: u32,
        time_index: u32,
        value_offset: u32,
        state: FstDumpState,
    ) -> Result<Self> {
        if value_offset > VALUE_OFFSET_MASK {
            return Err(crate::FstWriteError::InvalidSignalChanges(
                "value arena exceeds 1 GiB".to_string(),
            ));
        }
        Self::new(signal, time_index, value_offset, state)
    }

    fn new(signal: u32, time_index: u32, payload: u32, state: FstDumpState) -> Result<Self> {
        Ok(Self {
            record: FstSignalRecord::new(signal, time_index, payload, state)?,
            next: FST_NO_CHANGE,
        })
    }

    pub fn signal(&self) -> u32 {
        self.record.signal()
    }

    pub fn dump_state(&self) -> FstDumpState {
        self.record.dump_state()
    }

    pub fn set_dump_state(&mut self, state: FstDumpState) {
        self.record.set_dump_state(state);
    }

    pub fn time_index(&self) -> u32 {
        self.record.time_index()
    }

    pub fn set_time_index(&mut self, time_index: u32) {
        self.record.set_time_index(time_index);
    }

    pub fn next(&self) -> u32 {
        self.next
    }

    pub fn set_next(&mut self, next: u32) {
        self.next = next;
    }

    pub fn is_inline(&self) -> bool {
        self.record.is_inline()
    }

    pub fn is_packed_binary(&self) -> bool {
        self.record.is_packed_binary()
    }

    pub fn inline_value(&self) -> u8 {
        self.record.inline_value()
    }

    pub fn value_offset(&self) -> u32 {
        self.record.value_offset()
    }

    pub fn rebase_value_offset(&mut self, base: u32) -> Result<()> {
        self.record.rebase_value_offset(base)
    }

    pub(crate) fn record(&self) -> &FstSignalRecord {
        &self.record
    }
}

pub fn open_fst<P: AsRef<std::path::Path>>(
    path: P,
    info: &FstInfo,
) -> Result<FstHeaderWriter<std::io::BufWriter<std::fs::File>>> {
    FstHeaderWriter::open(path, info)
}

pub struct FstHeaderWriter<W: std::io::Write + std::io::Seek> {
    out: W,
    /// collect hierarchy section before compressing it
    hierarchy_buf: std::io::Cursor<Vec<u8>>,
    signals: Vec<FstSignalType>,
    scope_depth: u64,
    var_count: u64,
    scope_count: u64,
}

impl FstHeaderWriter<std::io::BufWriter<std::fs::File>> {
    fn open<P: AsRef<std::path::Path>>(path: P, info: &FstInfo) -> Result<Self> {
        let f = std::fs::File::create(path)?;
        let mut out = std::io::BufWriter::new(f);
        write_header_meta_data(&mut out, info)?;
        Ok(Self {
            out,
            hierarchy_buf: std::io::Cursor::new(Vec::new()),
            signals: vec![],
            scope_depth: 0,
            var_count: 0,
            scope_count: 0,
        })
    }
}

impl<W: std::io::Write + std::io::Seek> FstHeaderWriter<W> {
    pub fn scope(
        &mut self,
        name: impl AsRef<str>,
        component: impl AsRef<str>,
        tpe: FstScopeType,
    ) -> Result<()> {
        self.scope_depth += 1;
        self.scope_count += 1;
        write_hierarchy_scope(&mut self.hierarchy_buf, name, component, tpe)
    }
    pub fn up_scope(&mut self) -> Result<()> {
        debug_assert!(self.scope_depth > 0, "no scope to pop");
        self.scope_depth -= 1;
        write_hierarchy_up_scope(&mut self.hierarchy_buf)
    }

    pub fn var(
        &mut self,
        name: impl AsRef<str>,
        signal_tpe: FstSignalType,
        tpe: FstVarType,
        dir: FstVarDirection,
        alias: Option<FstSignalId>,
    ) -> Result<FstSignalId> {
        self.var_count += 1;
        write_hierarchy_var(&mut self.hierarchy_buf, tpe, dir, name, signal_tpe, alias)?;
        if let Some(alias) = alias {
            debug_assert!(alias.to_index() <= self.signals.len() as u32);
            Ok(alias)
        } else {
            self.signals.push(signal_tpe);
            let id = FstSignalId::from_index(self.signals.len() as u32);
            Ok(id)
        }
    }

    pub fn finish(self) -> Result<FstBodyWriter<W>> {
        let (encoder, writer) = self.finish_split()?;
        Ok(FstBodyWriter { encoder, writer })
    }

    /// Finish the immutable file prefix and return independent section
    /// encoding and ordered-output stages.
    pub fn finish_split(mut self) -> Result<(FstSectionEncoder, OrderedFstWriter<W>)> {
        debug_assert_eq!(
            self.scope_depth, 0,
            "missing calls to up-scope to close all scopes!"
        );
        write_hierarchy_bytes(&mut self.out, &self.hierarchy_buf.into_inner())?;
        write_geometry(&mut self.out, &self.signals)?;
        let encoder = FstSectionEncoder {
            buffer: SignalBuffer::new(&self.signals)?,
            signals: self.signals.clone(),
            compression_pool: None,
        };
        let finish_info = HeaderFinishInfo {
            end_time: 0, // currently unknown
            scope_count: self.scope_count,
            var_count: self.var_count,
            num_signals: self.signals.len() as u64,
            num_value_change_sections: 0, // currently unknown
        };
        let writer = OrderedFstWriter {
            out: self.out,
            finish_info,
        };
        Ok((encoder, writer))
    }
}

/// A complete, self-contained value-change section encoded in memory.
pub struct EncodedFstSection {
    bytes: Vec<u8>,
    end_time: u64,
    compression_cpu_seconds: f64,
    pack_cpu_seconds: f64,
    packer_input_bytes: usize,
    worker_cpu_seconds: Vec<f64>,
    recycled_capacity_bytes: usize,
    newly_allocated_capacity_bytes: usize,
    arena_to_packer_copied_bytes: usize,
}

impl EncodedFstSection {
    pub fn len(&self) -> usize {
        self.bytes.len()
    }

    pub fn is_empty(&self) -> bool {
        self.bytes.is_empty()
    }

    pub fn end_time(&self) -> u64 {
        self.end_time
    }

    pub fn compression_cpu_seconds(&self) -> f64 {
        self.compression_cpu_seconds
    }

    pub fn pack_cpu_seconds(&self) -> f64 {
        self.pack_cpu_seconds
    }

    pub fn packer_input_bytes(&self) -> usize {
        self.packer_input_bytes
    }

    pub fn worker_cpu_seconds(&self) -> &[f64] {
        &self.worker_cpu_seconds
    }

    pub fn recycled_capacity_bytes(&self) -> usize {
        self.recycled_capacity_bytes
    }

    pub fn newly_allocated_capacity_bytes(&self) -> usize {
        self.newly_allocated_capacity_bytes
    }

    pub fn arena_to_packer_copied_bytes(&self) -> usize {
        self.arena_to_packer_copied_bytes
    }
}

/// Stateful value-change encoder with no ownership of the output file.
pub struct FstSectionEncoder {
    buffer: SignalBuffer,
    signals: Vec<FstSignalType>,
    compression_pool: Option<Arc<rayon::ThreadPool>>,
}

impl FstSectionEncoder {
    /// Create a fresh encoder with first-file-section initialization semantics.
    pub fn fresh(&self) -> Result<Self> {
        Ok(Self {
            buffer: SignalBuffer::new(&self.signals)?,
            signals: self.signals.clone(),
            compression_pool: self.compression_pool.clone(),
        })
    }

    pub fn fresh_signal_chains(&self) -> Result<FstSignalChainEncoder> {
        Ok(FstSignalChainEncoder {
            buffer: ChainedSignalBuffer::new(&self.signals)?,
            compression_pool: self.compression_pool.clone(),
        })
    }

    pub fn signal_chains_from_frame(
        &self,
        frame: &[u8],
        start_time: u64,
    ) -> Result<FstSignalChainEncoder> {
        Ok(FstSignalChainEncoder {
            buffer: ChainedSignalBuffer::from_frame(&self.signals, frame, start_time)?,
            compression_pool: self.compression_pool.clone(),
        })
    }

    /// Use a persistent worker pool to compress independent signal streams.
    /// One worker preserves the serial flush path and creates no pool.
    pub fn set_compression_workers(&mut self, worker_count: usize) -> Result<()> {
        self.compression_pool = if worker_count <= 1 {
            None
        } else {
            Some(Arc::new(
                rayon::ThreadPoolBuilder::new()
                    .num_threads(worker_count)
                    .thread_name(|index| format!("fst-pack-{index}"))
                    .build()
                    .map_err(|error| std::io::Error::other(error.to_string()))?,
            ))
        };
        Ok(())
    }

    pub fn time_change(&mut self, time: u64) -> Result<()> {
        self.buffer.time_change(time)
    }

    pub fn signal_change(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.buffer.signal_change(signal_id, value)
    }

    /// Record a value already normalized to the signal's exact width.
    pub fn signal_change_exact(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.buffer.signal_change_exact(signal_id, value)
    }

    pub fn size(&self) -> usize {
        self.buffer.size()
    }

    pub fn encode_section(&mut self) -> Result<EncodedFstSection> {
        let (bytes, end_time, compression_cpu_seconds) =
            self.buffer.encode(self.compression_pool.as_deref())?;
        Ok(EncodedFstSection {
            bytes,
            end_time,
            compression_cpu_seconds,
            pack_cpu_seconds: 0.0,
            packer_input_bytes: 0,
            worker_cpu_seconds: Vec::new(),
            recycled_capacity_bytes: 0,
            newly_allocated_capacity_bytes: 0,
            arena_to_packer_copied_bytes: 0,
        })
    }

    /// Pack section-local linked change chains directly into final per-signal
    /// streams. Signal chains are independent and are processed on the
    /// persistent compression pool when one is configured.
    #[allow(clippy::too_many_arguments)]
    pub fn encode_signal_chains(
        &self,
        incoming_frame: &[u8],
        start_time: u64,
        end_time: u64,
        first_file_section: bool,
        time_points: &[u64],
        first_by_signal: &[u32],
        changes: &[FstSignalChange],
        values: &[u8],
        incoming_dump_enabled: bool,
    ) -> Result<EncodedFstSection> {
        let (bytes, stats) = write_chained_value_change_section(
            &self.signals,
            incoming_frame,
            start_time,
            end_time,
            first_file_section,
            time_points,
            first_by_signal,
            changes,
            values,
            incoming_dump_enabled,
            self.compression_pool.as_deref(),
        )?;
        Ok(EncodedFstSection {
            bytes,
            end_time,
            compression_cpu_seconds: stats.compression_cpu_seconds,
            pack_cpu_seconds: stats.pack_cpu_seconds,
            packer_input_bytes: stats.packer_input_bytes,
            worker_cpu_seconds: stats.worker_cpu_seconds,
            recycled_capacity_bytes: stats.recycled_capacity_bytes,
            newly_allocated_capacity_bytes: stats.newly_allocated_capacity_bytes,
            arena_to_packer_copied_bytes: 0,
        })
    }

    /// Create an independent encoder initialized from an incoming full frame.
    pub fn from_frame(&self, frame: &[u8], start_time: u64) -> Result<Self> {
        Ok(Self {
            buffer: SignalBuffer::from_frame(&self.signals, frame, start_time)?,
            signals: self.signals.clone(),
            compression_pool: self.compression_pool.clone(),
        })
    }
}

/// Incremental section encoder for chunk-local linked signal changes.
pub struct FstSignalChainEncoder {
    buffer: ChainedSignalBuffer,
    compression_pool: Option<Arc<rayon::ThreadPool>>,
}

impl FstSignalChainEncoder {
    pub fn time_change(&mut self, time: u64) -> Result<u32> {
        self.buffer.time_change(time)
    }

    pub fn current_time_index(&self) -> u32 {
        self.buffer.current_time_index()
    }

    pub fn apply_signal_chains(
        &mut self,
        first_by_signal: &[u32],
        changes: &[FstSignalChange],
        values: &[u8],
    ) -> Result<()> {
        self.buffer.apply_signal_chains(
            first_by_signal,
            changes,
            values,
            self.compression_pool.as_deref(),
        )
    }

    pub fn apply_signal_records(
        &mut self,
        changes: &[FstSignalRecord],
        values: &[u8],
    ) -> Result<()> {
        self.buffer
            .apply_signal_records(changes, values, self.compression_pool.as_deref())
    }

    pub fn size(&self) -> usize {
        self.buffer.size()
    }

    pub fn encode_section(&mut self) -> Result<EncodedFstSection> {
        let (bytes, end_time, compression_cpu_seconds, stats) =
            self.buffer.encode(self.compression_pool.as_deref())?;
        Ok(EncodedFstSection {
            bytes,
            end_time,
            compression_cpu_seconds,
            pack_cpu_seconds: stats.pack_cpu_seconds,
            packer_input_bytes: stats.packer_to_compressor_bytes,
            worker_cpu_seconds: stats.worker_cpu_seconds,
            recycled_capacity_bytes: 0,
            newly_allocated_capacity_bytes: 0,
            arena_to_packer_copied_bytes: stats.arena_to_packer_copied_bytes,
        })
    }
}

/// Owns ordered section append and the final FST header patch.
pub struct OrderedFstWriter<W: std::io::Write + std::io::Seek> {
    out: W,
    finish_info: HeaderFinishInfo,
}

impl<W: std::io::Write + std::io::Seek> OrderedFstWriter<W> {
    pub fn append_section(&mut self, section: EncodedFstSection) -> Result<()> {
        self.out.write_all(&section.bytes)?;
        self.finish_info.num_value_change_sections += 1;
        self.finish_info.end_time = section.end_time;
        Ok(())
    }

    pub fn finish(mut self) -> Result<()> {
        update_header(&mut self.out, &self.finish_info)
    }
}

pub struct FstBodyWriter<W: std::io::Write + std::io::Seek> {
    encoder: FstSectionEncoder,
    writer: OrderedFstWriter<W>,
}

impl<W: std::io::Write + std::io::Seek> FstBodyWriter<W> {
    pub fn time_change(&mut self, time: u64) -> Result<()> {
        self.encoder.time_change(time)
    }

    pub fn signal_change(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.encoder.signal_change(signal_id, value)
    }

    /// flushes all value change data to disk
    pub fn flush(&mut self) -> Result<()> {
        let section = self.encoder.encode_section()?;
        self.writer.append_section(section)
    }

    /// Returns the estimated size of all data structures that grow over time.
    pub fn size(&self) -> usize {
        self.encoder.size()
    }

    pub fn finish(mut self) -> Result<()> {
        let section = self.encoder.encode_section()?;
        self.writer.append_section(section)?;
        self.writer.finish()
    }
}
