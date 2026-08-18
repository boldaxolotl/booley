// Copyright 2024 Cornell University
// released under BSD 3-Clause License
// author: Kevin Laeufer <laeufer@cornell.edu>

use std::num::NonZeroU32;

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FstFileType {
    Verilog = 0,
    Vhdl = 1,
    VerilogVhdl = 2,
}

#[derive(Debug, Clone)]
pub struct FstInfo {
    pub start_time: u64,
    // TODO: better abstraction
    /// All times in the file are stored in units of 10^timescale_exponent s.
    pub timescale_exponent: i8,
    pub version: String,
    pub date: String,
    pub file_type: FstFileType,
}

#[derive(Debug, Copy, Clone, Ord, PartialOrd, Eq, PartialEq, Hash)]
pub struct FstSignalId(NonZeroU32);

impl FstSignalId {
    pub(crate) fn from_index(index: u32) -> Self {
        FstSignalId(NonZeroU32::new(index).unwrap())
    }

    /// The raw value used in the FST file format.
    pub(crate) fn to_index(self) -> u32 {
        self.0.get()
    }

    pub(crate) fn to_array_index(self) -> usize {
        self.0.get() as usize - 1
    }
}

#[derive(Debug, Copy, Clone, PartialEq)]
pub struct FstSignalType(SignalType);

#[derive(Debug, Copy, Clone, PartialEq)]
enum SignalType {
    BitVec(NonZeroU32),
    Real,
}

impl FstSignalType {
    pub fn bit_vec(len: u32) -> Self {
        Self(SignalType::BitVec(NonZeroU32::new(len + 1).unwrap()))
    }

    pub fn real() -> Self {
        Self(SignalType::Real)
    }

    pub(crate) fn to_file_format(self) -> u32 {
        match self.0 {
            SignalType::BitVec(value) => match value.get() {
                1 => u32::MAX,
                other => other - 1,
            },
            SignalType::Real => 0,
        }
    }

    #[inline]
    pub(crate) fn len(&self) -> u32 {
        match self.0 {
            SignalType::BitVec(value) => value.get() - 1,
            SignalType::Real => 8,
        }
    }
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FstScopeType {
    // VCD
    Module = 0,
    Task = 1,
    Function = 2,
    Begin = 3,
    Fork = 4,
    Generate = 5,
    Struct = 6,
    Union = 7,
    Class = 8,
    Interface = 9,
    Package = 10,
    Program = 11,
    // VHDL
    VhdlArchitecture = 12,
    VhdlProcedure = 13,
    VhdlFunction = 14,
    VhdlRecord = 15,
    VhdlProcess = 16,
    VhdlBlock = 17,
    VhdlForGenerate = 18,
    VhdlIfGenerate = 19,
    VhdlGenerate = 20,
    VhdlPackage = 21,
}

#[repr(u8)]
#[derive(Debug, PartialEq, Copy, Clone)]
pub enum FstVarType {
    // VCD
    Event = 0,
    Integer = 1,
    Parameter = 2,
    Real = 3,
    RealParameter = 4,
    Reg = 5,
    Supply0 = 6,
    Supply1 = 7,
    Time = 8,
    Tri = 9,
    TriAnd = 10,
    TriOr = 11,
    TriReg = 12,
    Tri0 = 13,
    Tri1 = 14,
    Wand = 15, // or WAnd ?
    Wire = 16,
    Wor = 17, // or WOr?
    Port = 18,
    SparseArray = 19,
    RealTime = 20,
    GenericString = 21,
    // System Verilog
    Bit = 22,
    Logic = 23,
    Int = 24,
    ShortInt = 25,
    LongInt = 26,
    Byte = 27,
    Enum = 28,
    ShortReal = 29,
}

#[repr(u8)]
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum FstVarDirection {
    Implicit = 0,
    Input = 1,
    Output = 2,
    InOut = 3,
    Buffer = 4,
    Linkage = 5,
}
