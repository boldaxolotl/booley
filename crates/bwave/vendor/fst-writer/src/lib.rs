// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

mod buffer;
mod io;
mod types;
mod writer;

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
    #[error("Invalid bit-vector signal character: {0}")]
    InvalidCharacter(char),
}

pub use types::*;
pub use writer::{FstBodyWriter, FstHeaderWriter, open_fst};
