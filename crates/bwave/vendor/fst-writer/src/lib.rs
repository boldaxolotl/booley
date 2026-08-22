// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

mod buffer;
mod io;
mod types;
mod writer;

use std::time::Duration;

type Result<T> = std::result::Result<T, FstWriteError>;

#[derive(Debug, thiserror::Error)]
pub enum FstWriteError {
    #[error("I/O operation failed")]
    Io(#[from] std::io::Error),
    #[error("The string is too large (max length: {0}): {1}")]
    StringTooLong(usize, String),
    #[error("Cannot change the time from {0} to {1}. Time must always increase!")]
    TimeDecrease(u64, u64),
    #[error("Invalid signal id: {0:?}")]
    InvalidSignalId(FstSignalId),
    #[error("Initial frame has {actual} bytes; expected {expected}")]
    InvalidFrameLength { expected: usize, actual: usize },
    #[error("Invalid bit-vector signal character: {0}")]
    InvalidCharacter(char),
}

/// Controls compression of value-change streams and time tables.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum FstCompression {
    Disabled,
    #[default]
    Enabled,
}

/// Optional aggregate statistics collected while value-change sections flush.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct FstWriteStats {
    pub sections: u64,
    pub uncompressed_stream_bytes: u64,
    /// Bytes stored after per-stream compression decisions.
    pub compressed_stream_bytes: u64,
    pub flush_time: Duration,
}

pub use types::*;
pub use writer::{
    EncodedFstSection, FstBodyWriter, FstHeaderWriter, FstSectionEncoder, OrderedFstWriter,
    open_fst,
};
