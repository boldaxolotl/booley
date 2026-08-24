//! Bounded, timestamp-aligned VCD body chunks shared by file and FIFO inputs.

use std::io::Read;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use memchr::memchr;

use crate::parser::VcdParseError;

// Once the target is full, read only a narrow suffix while searching for the
// next timestamp. A 1 MiB suffix made every chunk copy a large remainder
// twice (split + recycled-buffer activation), which was visible reader work
// on dense simulator traces. Initial reads still request the whole target.
const READ_BLOCK_BYTES: usize = 64 * 1024;
// One reader can consume only one buffer at a time. A small cushion absorbs
// bursts of parser completions without retaining tens of chunk-sized
// allocations (64 x 32 MiB would violate the process RSS contract alone).
const MAX_RECYCLED_BUFFERS: usize = 4;

#[derive(Debug, Eq, PartialEq)]
pub struct VcdChunk {
    pub sequence: u64,
    pub start_offset: u64,
    pub end_offset: u64,
    pub bytes: Vec<u8>,
}

pub struct VcdChunkSource<R> {
    reader: R,
    target_bytes: usize,
    buffer: Vec<u8>,
    next_offset: u64,
    sequence: u64,
    eof: bool,
    boundary_search: usize,
    recycled: Vec<Vec<u8>>,
    cancellation: Option<Arc<AtomicBool>>,
}

impl<R: Read> VcdChunkSource<R> {
    pub fn new(reader: R, body_offset: u64, target_bytes: usize) -> Result<Self, String> {
        if target_bytes == 0 {
            return Err("VCD chunk target must be positive".to_string());
        }
        Ok(Self {
            reader,
            target_bytes,
            buffer: Vec::with_capacity(target_bytes.saturating_add(READ_BLOCK_BYTES)),
            next_offset: body_offset,
            sequence: 0,
            eof: false,
            boundary_search: target_bytes.max(1),
            recycled: Vec::new(),
            cancellation: None,
        })
    }

    pub fn with_cancellation(mut self, cancellation: Arc<AtomicBool>) -> Self {
        self.cancellation = Some(cancellation);
        self
    }

    pub fn next_chunk(&mut self) -> Result<Option<VcdChunk>, VcdParseError> {
        loop {
            self.check_cancelled()?;
            if let Some(boundary) = self.next_boundary() {
                return self.take_chunk(boundary).map(Some);
            }
            if self.eof {
                if self.buffer.is_empty() {
                    return Ok(None);
                }
                return self.take_chunk(self.buffer.len()).map(Some);
            }
            self.read_block()?;
        }
    }

    /// Return a consumed chunk buffer to the source's bounded reuse pool.
    pub fn recycle(&mut self, mut chunk: VcdChunk) {
        chunk.bytes.clear();
        chunk.bytes.extend_from_slice(&self.buffer);
        let mut previous = std::mem::replace(&mut self.buffer, chunk.bytes);
        previous.clear();
        if previous.capacity() >= self.target_bytes && self.recycled.len() < MAX_RECYCLED_BUFFERS {
            self.recycled.push(previous);
        }
    }

    fn check_cancelled(&self) -> Result<(), VcdParseError> {
        self.check_cancelled_at(self.next_offset + self.buffer.len() as u64)
    }

    fn check_cancelled_at(&self, offset: u64) -> Result<(), VcdParseError> {
        if self
            .cancellation
            .as_ref()
            .is_some_and(|flag| flag.load(Ordering::Relaxed))
        {
            return Err(VcdParseError::Cancelled {
                section: "body",
                offset,
            });
        }
        Ok(())
    }

    fn next_boundary(&mut self) -> Option<usize> {
        if self.buffer.len() < self.target_bytes {
            return None;
        }
        let mut search = self.boundary_search;
        while search < self.buffer.len() {
            let Some(relative) = memchr(b'#', &self.buffer[search..]) else {
                self.boundary_search = self.buffer.len();
                return None;
            };
            let position = search + relative;
            let starts_line = self.buffer[position - 1] == b'\n';
            let complete = self.eof || memchr(b'\n', &self.buffer[position..]).is_some();
            if starts_line && complete {
                return Some(position);
            }
            if starts_line {
                self.boundary_search = position;
                return None;
            }
            search = position + 1;
        }
        self.boundary_search = search;
        None
    }

    fn read_block(&mut self) -> Result<(), VcdParseError> {
        self.activate_recycled_buffer();
        let old_len = self.buffer.len();
        let requested = self
            .target_bytes
            .saturating_sub(old_len)
            .max(READ_BLOCK_BYTES);
        self.buffer.resize(old_len + requested, 0);
        let read = loop {
            self.check_cancelled_at(self.next_offset + old_len as u64)?;
            match self.reader.read(&mut self.buffer[old_len..]) {
                Ok(read) => break read,
                Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                    std::thread::sleep(Duration::from_millis(2));
                }
                Err(source) => {
                    return Err(VcdParseError::Read {
                        section: "body",
                        offset: self.next_offset + old_len as u64,
                        source,
                    });
                }
            }
        };
        self.buffer.truncate(old_len + read);
        self.eof = read == 0;
        Ok(())
    }

    fn activate_recycled_buffer(&mut self) {
        if self.buffer.capacity() >= self.target_bytes {
            return;
        }
        let Some(mut recycled) = self.recycled.pop() else {
            return;
        };
        recycled.extend_from_slice(&self.buffer);
        self.buffer = recycled;
    }

    fn take_chunk(&mut self, boundary: usize) -> Result<VcdChunk, VcdParseError> {
        let remainder = self.buffer.split_off(boundary);
        let bytes = std::mem::replace(&mut self.buffer, remainder);
        let end_offset = self
            .next_offset
            .checked_add(bytes.len() as u64)
            .ok_or_else(|| VcdParseError::Read {
                section: "body",
                offset: self.next_offset,
                source: std::io::Error::other("VCD input offset exceeds u64"),
            })?;
        let chunk = VcdChunk {
            sequence: self.sequence,
            start_offset: self.next_offset,
            end_offset,
            bytes,
        };
        self.sequence += 1;
        self.next_offset = end_offset;
        self.boundary_search = self.target_bytes.max(1);
        Ok(chunk)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, Cursor};

    struct ShortReader<R> {
        inner: R,
        max_read: usize,
    }

    impl<R: Read> Read for ShortReader<R> {
        fn read(&mut self, output: &mut [u8]) -> io::Result<usize> {
            let limit = output.len().min(self.max_read);
            self.inner.read(&mut output[..limit])
        }
    }

    struct ErrorReader;

    impl Read for ErrorReader {
        fn read(&mut self, _output: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("injected chunk read failure"))
        }
    }

    struct BlockedReader;

    impl Read for BlockedReader {
        fn read(&mut self, _output: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::from(io::ErrorKind::WouldBlock))
        }
    }

    fn collect<R: Read>(reader: R, target: usize) -> Result<Vec<VcdChunk>, VcdParseError> {
        let mut source = VcdChunkSource::new(reader, 100, target).unwrap();
        let mut chunks = Vec::new();
        while let Some(chunk) = source.next_chunk()? {
            chunks.push(chunk);
        }
        Ok(chunks)
    }

    #[test]
    fn concatenation_is_exact_and_nonfirst_chunks_start_at_timestamps() {
        let body = b"#0\r\n0!\r\n#10\r\n1!\r\n#20\r\n0!";
        for target in 1..body.len() + 3 {
            let chunks = collect(Cursor::new(body), target).unwrap();
            let rebuilt: Vec<u8> = chunks
                .iter()
                .flat_map(|chunk| chunk.bytes.clone())
                .collect();
            assert_eq!(rebuilt, body);
            assert!(chunks.iter().skip(1).all(|chunk| chunk.bytes[0] == b'#'));
            assert_eq!(chunks.first().unwrap().start_offset, 100);
            assert_eq!(chunks.last().unwrap().end_offset, 100 + body.len() as u64);
        }
    }

    #[test]
    fn timestamp_lines_may_be_split_across_reads() {
        let body = b"#0\n0!\n#123456789\n1!\n#20\n0!\n";
        let reader = ShortReader {
            inner: Cursor::new(body),
            max_read: 2,
        };
        let chunks = collect(reader, 6).unwrap();
        assert_eq!(chunks.len(), 3);
        assert_eq!(chunks[1].bytes, b"#123456789\n1!\n");
        assert_eq!(chunks[2].bytes, b"#20\n0!\n");
    }

    #[test]
    fn huge_line_becomes_part_of_one_oversized_chunk() {
        let mut body = b"#0\n".to_vec();
        body.extend(std::iter::repeat_n(b'x', READ_BLOCK_BYTES + 17));
        body.extend_from_slice(b"\n#1\n1!\n");
        let chunks = collect(Cursor::new(&body), 32).unwrap();
        assert_eq!(chunks.len(), 2);
        assert!(chunks[0].bytes.len() > READ_BLOCK_BYTES);
        assert_eq!(chunks[1].bytes, b"#1\n1!\n");
    }

    #[test]
    fn empty_body_and_unterminated_final_line_are_preserved() {
        assert!(collect(Cursor::new(b""), 8).unwrap().is_empty());
        let chunks = collect(Cursor::new(b"#0\n1!"), 8).unwrap();
        assert_eq!(chunks[0].bytes, b"#0\n1!");
    }

    #[test]
    fn read_errors_keep_the_absolute_input_offset() {
        let error = collect(ErrorReader, 8).unwrap_err();
        assert!(matches!(
            error,
            VcdParseError::Read {
                section: "body",
                offset: 100,
                ..
            }
        ));
    }

    #[test]
    fn cancellation_is_observed_before_reading() {
        let cancelled = Arc::new(AtomicBool::new(true));
        let mut source = VcdChunkSource::new(Cursor::new(b"#0\n"), 44, 8)
            .unwrap()
            .with_cancellation(cancelled);
        assert!(matches!(
            source.next_chunk().unwrap_err(),
            VcdParseError::Cancelled {
                section: "body",
                offset: 44
            }
        ));
    }

    #[test]
    fn cancellation_wakes_a_nonblocking_fifo_read() {
        let cancelled = Arc::new(AtomicBool::new(false));
        let worker_flag = Arc::clone(&cancelled);
        let worker = std::thread::spawn(move || {
            let mut source = VcdChunkSource::new(BlockedReader, 44, 8)
                .unwrap()
                .with_cancellation(worker_flag);
            source.next_chunk().unwrap_err()
        });
        std::thread::sleep(Duration::from_millis(10));
        cancelled.store(true, Ordering::Relaxed);
        assert!(matches!(
            worker.join().unwrap(),
            VcdParseError::Cancelled {
                section: "body",
                offset: 44
            }
        ));
    }
}
