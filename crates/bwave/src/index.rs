//! Cycle-based index for fast seeking into large VCD files.
//!
//! Index format (.idx sidecar):
//!   magic:              [u8; 4]  = b"VCDI"
//!   version:            u32
//!   interval:           u64      (cycles between entries)
//!   count:              u64      (number of entries)
//!   clock_period_ticks: u64
//!   first_rise_tick:    u64
//!   clock_id_len:       u16
//!   clock_id:           [u8; clock_id_len]
//!   entries:            count * (u64 cycle, u64 byte_offset)

use std::fs;
use std::io::{self, Write};
use std::path::Path;

const MAGIC: &[u8; 4] = b"VCDI";
const VERSION: u32 = 1;

pub struct CycleIndex {
    pub interval: u64,
    pub entries: Vec<(u64, u64)>,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
    pub clock_id: String,
}

pub struct SeekInfo {
    pub byte_offset: u64,
    pub start_cycle: u64,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
}

/// Builder that accumulates index entries during a full scan.
pub struct IndexBuilder {
    pub interval: u64,
    pub entries: Vec<(u64, u64)>,
    pub clock_id: String,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
}

impl IndexBuilder {
    pub fn new(interval: u64) -> Self {
        IndexBuilder {
            interval,
            entries: Vec::new(),
            clock_id: String::new(),
            clock_period_ticks: 0,
            first_rise_tick: 0,
        }
    }

    pub fn record(&mut self, cycle: u64, byte_offset: u64) {
        if cycle > 0 && cycle % self.interval == 0 {
            self.entries.push((cycle, byte_offset));
        }
    }

    pub fn into_index(self) -> Option<CycleIndex> {
        if self.entries.is_empty() || self.clock_period_ticks == 0 {
            return None;
        }
        Some(CycleIndex {
            interval: self.interval,
            entries: self.entries,
            clock_period_ticks: self.clock_period_ticks,
            first_rise_tick: self.first_rise_tick,
            clock_id: self.clock_id,
        })
    }
}

impl CycleIndex {
    pub fn write_to_file(&self, vcd_path: &Path) -> io::Result<()> {
        let idx_path = vcd_path.with_extension("vcd.idx");
        let mut f = fs::File::create(&idx_path)?;
        f.write_all(MAGIC)?;
        f.write_all(&VERSION.to_le_bytes())?;
        f.write_all(&self.interval.to_le_bytes())?;
        f.write_all(&(self.entries.len() as u64).to_le_bytes())?;
        f.write_all(&self.clock_period_ticks.to_le_bytes())?;
        f.write_all(&self.first_rise_tick.to_le_bytes())?;
        let id_bytes = self.clock_id.as_bytes();
        f.write_all(&(id_bytes.len() as u16).to_le_bytes())?;
        f.write_all(id_bytes)?;
        for &(cycle, offset) in &self.entries {
            f.write_all(&cycle.to_le_bytes())?;
            f.write_all(&offset.to_le_bytes())?;
        }
        Ok(())
    }

    pub fn read_from_file(vcd_path: &Path) -> Option<CycleIndex> {
        let idx_path = vcd_path.with_extension("vcd.idx");
        if !idx_path.exists() {
            return None;
        }
        // Freshness: idx must be newer than VCD
        let vcd_modified = fs::metadata(vcd_path).ok()?.modified().ok()?;
        let idx_modified = fs::metadata(&idx_path).ok()?.modified().ok()?;
        if idx_modified < vcd_modified {
            return None;
        }
        let data = fs::read(&idx_path).ok()?;
        Self::parse_bytes(&data)
    }

    fn parse_bytes(data: &[u8]) -> Option<CycleIndex> {
        #[allow(unused_assignments)]
        let mut pos = 0;
        let read_u16 = |data: &[u8], pos: &mut usize| -> Option<u16> {
            if *pos + 2 > data.len() {
                return None;
            }
            let val = u16::from_le_bytes(data[*pos..*pos + 2].try_into().ok()?);
            *pos += 2;
            Some(val)
        };
        let read_u32 = |data: &[u8], pos: &mut usize| -> Option<u32> {
            if *pos + 4 > data.len() {
                return None;
            }
            let val = u32::from_le_bytes(data[*pos..*pos + 4].try_into().ok()?);
            *pos += 4;
            Some(val)
        };
        let read_u64 = |data: &[u8], pos: &mut usize| -> Option<u64> {
            if *pos + 8 > data.len() {
                return None;
            }
            let val = u64::from_le_bytes(data[*pos..*pos + 8].try_into().ok()?);
            *pos += 8;
            Some(val)
        };

        if data.len() < 4 || &data[0..4] != MAGIC {
            return None;
        }
        pos = 4;
        let version = read_u32(data, &mut pos)?;
        if version != VERSION {
            return None;
        }
        let interval = read_u64(data, &mut pos)?;
        let count = read_u64(data, &mut pos)?;
        let clock_period_ticks = read_u64(data, &mut pos)?;
        let first_rise_tick = read_u64(data, &mut pos)?;
        let id_len = read_u16(data, &mut pos)? as usize;
        if pos + id_len > data.len() {
            return None;
        }
        let clock_id = String::from_utf8(data[pos..pos + id_len].to_vec()).ok()?;
        pos += id_len;

        let mut entries = Vec::with_capacity(count as usize);
        for _ in 0..count {
            let cycle = read_u64(data, &mut pos)?;
            let offset = read_u64(data, &mut pos)?;
            entries.push((cycle, offset));
        }

        Some(CycleIndex {
            interval,
            entries,
            clock_period_ticks,
            first_rise_tick,
            clock_id,
        })
    }

    /// Find the best seek point: the entry at or before target_cycle.
    pub fn seek_for_cycle(&self, target_cycle: u64) -> Option<SeekInfo> {
        if self.entries.is_empty() {
            return None;
        }
        // Binary search for largest entry cycle <= target_cycle
        let idx = match self
            .entries
            .binary_search_by_key(&target_cycle, |&(c, _)| c)
        {
            Ok(i) => i,
            Err(0) => return None, // target before first entry
            Err(i) => i - 1,
        };
        let (cycle, offset) = self.entries[idx];
        Some(SeekInfo {
            byte_offset: offset,
            start_cycle: cycle,
            clock_period_ticks: self.clock_period_ticks,
            first_rise_tick: self.first_rise_tick,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;

    #[test]
    fn test_index_roundtrip() {
        let dir = std::env::temp_dir().join("vcd_idx_test");
        let _ = fs::create_dir_all(&dir);
        let vcd_path = dir.join("test.vcd");
        // Create a dummy VCD file
        let mut f = fs::File::create(&vcd_path).unwrap();
        write!(f, "dummy").unwrap();
        drop(f);

        let idx = CycleIndex {
            interval: 10000,
            entries: vec![(10000, 1000), (20000, 2000), (30000, 3000)],
            clock_period_ticks: 10,
            first_rise_tick: 5,
            clock_id: "!".to_string(),
        };
        idx.write_to_file(&vcd_path).unwrap();
        // Touch idx to be newer
        let idx_path = vcd_path.with_extension("vcd.idx");
        let data = fs::read(&idx_path).unwrap();
        let parsed = CycleIndex::parse_bytes(&data).unwrap();

        assert_eq!(parsed.interval, 10000);
        assert_eq!(parsed.entries.len(), 3);
        assert_eq!(parsed.entries[0], (10000, 1000));
        assert_eq!(parsed.clock_period_ticks, 10);
        assert_eq!(parsed.first_rise_tick, 5);
        assert_eq!(parsed.clock_id, "!");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_seek_binary_search() {
        let idx = CycleIndex {
            interval: 10000,
            entries: vec![(10000, 1000), (20000, 2000), (30000, 3000)],
            clock_period_ticks: 10,
            first_rise_tick: 5,
            clock_id: "!".to_string(),
        };
        // Exact match
        let s = idx.seek_for_cycle(20000).unwrap();
        assert_eq!(s.start_cycle, 20000);
        assert_eq!(s.byte_offset, 2000);

        // Between entries
        let s = idx.seek_for_cycle(25000).unwrap();
        assert_eq!(s.start_cycle, 20000);

        // Before first entry
        assert!(idx.seek_for_cycle(5000).is_none());

        // After last entry
        let s = idx.seek_for_cycle(99999).unwrap();
        assert_eq!(s.start_cycle, 30000);
    }

    #[test]
    fn test_builder_record() {
        let mut b = IndexBuilder::new(100);
        b.record(50, 500); // not on interval boundary
        b.record(100, 1000); // yes
        b.record(200, 2000); // yes
        b.record(0, 0); // cycle 0 skipped
        assert_eq!(b.entries.len(), 2);
        assert_eq!(b.entries[0], (100, 1000));
    }
}
