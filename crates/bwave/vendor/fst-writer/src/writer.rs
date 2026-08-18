// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

use crate::buffer::SignalBuffer;
use crate::io::{
    HeaderFinishInfo, update_header, write_geometry, write_header_meta_data, write_hierarchy_bytes,
    write_hierarchy_scope, write_hierarchy_up_scope, write_hierarchy_var,
};
use crate::{
    FstInfo, FstScopeType, FstSignalId, FstSignalType, FstVarDirection, FstVarType, Result,
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
        };
        Ok(next)
    }
}

pub struct FstBodyWriter<W: std::io::Write + std::io::Seek> {
    out: W,
    buffer: SignalBuffer,
    finish_info: HeaderFinishInfo,
}

impl<W: std::io::Write + std::io::Seek> FstBodyWriter<W> {
    pub fn time_change(&mut self, time: u64) -> Result<()> {
        self.buffer.time_change(time)
    }

    pub fn signal_change(&mut self, signal_id: FstSignalId, value: &[u8]) -> Result<()> {
        self.buffer.signal_change(signal_id, value)
    }

    /// flushes all value change data to disk
    pub fn flush(&mut self) -> Result<()> {
        self.buffer.flush(&mut self.out)?;
        self.finish_info.num_value_change_sections += 1;
        Ok(())
    }

    /// Returns the estimated size of all data structures that grow over time.
    pub fn size(&self) -> usize {
        self.buffer.size()
    }

    pub fn finish(mut self) -> Result<()> {
        // write value change section
        let end_time = self.buffer.flush(&mut self.out)?;

        // update info
        self.finish_info.num_value_change_sections += 1;
        self.finish_info.end_time = end_time;
        update_header(&mut self.out, &self.finish_info)?;

        Ok(())
    }
}
