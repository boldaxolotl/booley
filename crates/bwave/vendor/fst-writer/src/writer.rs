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
}

/// Stateful value-change encoder with no ownership of the output file.
pub struct FstSectionEncoder {
    buffer: SignalBuffer,
    signals: Vec<FstSignalType>,
}

impl FstSectionEncoder {
    /// Create a fresh encoder with first-file-section initialization semantics.
    pub fn fresh(&self) -> Result<Self> {
        Ok(Self {
            buffer: SignalBuffer::new(&self.signals)?,
            signals: self.signals.clone(),
        })
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
        let (bytes, end_time) = self.buffer.encode()?;
        Ok(EncodedFstSection { bytes, end_time })
    }

    /// Create an independent encoder initialized from an incoming full frame.
    pub fn from_frame(&self, frame: &[u8], start_time: u64) -> Result<Self> {
        Ok(Self {
            buffer: SignalBuffer::from_frame(&self.signals, frame, start_time)?,
            signals: self.signals.clone(),
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
