// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

use crate::buffer::SignalBuffer;
use crate::io::{
    HeaderFinishInfo, update_header, write_geometry, write_header_meta_data, write_hierarchy_bytes,
    write_hierarchy_scope, write_hierarchy_up_scope, write_hierarchy_var,
};
use crate::{
    FstCompression, FstInfo, FstScopeType, FstSignalId, FstSignalType, FstVarDirection, FstVarType,
    FstWriteStats, Result,
};

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
        Self::new(std::io::BufWriter::new(f), info)
    }
}

impl<W: std::io::Write + std::io::Seek> FstHeaderWriter<W> {
    /// Start an FST writer on any seekable output, including an in-memory cursor.
    pub fn new(mut out: W, info: &FstInfo) -> Result<Self> {
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

    pub fn finish(mut self) -> Result<FstBodyWriter<W>> {
        debug_assert_eq!(
            self.scope_depth, 0,
            "missing calls to up-scope to close all scopes!"
        );
        write_hierarchy_bytes(&mut self.out, &self.hierarchy_buf.into_inner())?;
        write_geometry(&mut self.out, &self.signals)?;
        let buffer = SignalBuffer::new(&self.signals)?;
        let finish_info = HeaderFinishInfo {
            end_time: 0, // currently unknown
            scope_count: self.scope_count,
            var_count: self.var_count,
            num_signals: self.signals.len() as u64,
            num_value_change_sections: 0, // currently unknown
        };
        let next = FstBodyWriter {
            out: self.out,
            buffer,
            finish_info,
            compression: FstCompression::Enabled,
            stats: None,
        };
        Ok(next)
    }
}

pub struct FstBodyWriter<W: std::io::Write + std::io::Seek> {
    out: W,
    buffer: SignalBuffer,
    finish_info: HeaderFinishInfo,
    compression: FstCompression,
    stats: Option<FstWriteStats>,
}

impl<W: std::io::Write + std::io::Seek> FstBodyWriter<W> {
    pub fn time_change(&mut self, time: u64) -> Result<()> {
        self.buffer.time_change(time)
    }

    pub fn signal_change(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.signal_change_with_status(signal_id, value).map(|_| ())
    }

    /// Encode a signal value and report whether it changed the current state.
    pub fn signal_change_with_status(
        &mut self,
        signal_id: FstSignalId,
        value: &[u8],
    ) -> Result<bool> {
        self.buffer.signal_change(signal_id, value)
    }

    /// Apply VCD width extension/truncation and encode a signal change in one
    /// writer operation.
    pub fn signal_change_vcd(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.signal_change_vcd_with_status(signal_id, value)
            .map(|_| ())
    }

    /// Apply VCD normalization and report whether the current value changed.
    pub fn signal_change_vcd_with_status(
        &mut self,
        signal_id: FstSignalId,
        value: &[u8],
    ) -> Result<bool> {
        self.buffer.signal_change_vcd(signal_id, value)
    }

    /// Select whether subsequent section flushes compress stream payloads.
    pub fn set_compression(&mut self, compression: FstCompression) {
        self.compression = compression;
    }

    /// Enable aggregate section statistics. Collection is disabled by default.
    pub fn enable_stats(&mut self) {
        self.stats.get_or_insert_with(FstWriteStats::default);
    }

    pub fn stats(&self) -> Option<&FstWriteStats> {
        self.stats.as_ref()
    }

    fn flush_buffer(&mut self) -> Result<u64> {
        let started = self.stats.as_ref().map(|_| std::time::Instant::now());
        let (end_time, section_stats) = self.buffer.flush(&mut self.out, self.compression)?;
        if let Some(stats) = self.stats.as_mut() {
            stats.sections += 1;
            stats.uncompressed_stream_bytes += section_stats.uncompressed_stream_bytes;
            stats.compressed_stream_bytes += section_stats.compressed_stream_bytes;
            stats.flush_time += started
                .expect("timer exists when stats are enabled")
                .elapsed();
        }
        Ok(end_time)
    }

    /// flushes all value change data to disk
    pub fn flush(&mut self) -> Result<()> {
        self.flush_buffer()?;
        self.finish_info.num_value_change_sections += 1;
        Ok(())
    }

    /// Returns the estimated size of all data structures that grow over time.
    pub fn size(&self) -> usize {
        self.buffer.size()
    }

    pub fn finish(self) -> Result<()> {
        self.finish_with_stats().map(|_| ())
    }

    pub fn finish_with_stats(mut self) -> Result<Option<FstWriteStats>> {
        // write value change section
        let end_time = self.flush_buffer()?;

        // update info
        self.finish_info.num_value_change_sections += 1;
        self.finish_info.end_time = end_time;
        update_header(&mut self.out, &self.finish_info)?;

        Ok(self.stats)
    }
}
