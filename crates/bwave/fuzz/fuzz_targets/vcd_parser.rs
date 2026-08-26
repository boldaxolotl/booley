#![no_main]

use std::io::{BufReader, Cursor};
use std::ops::ControlFlow;

use bwave::parser::{parse_streaming_with_offsets, try_parse_header, VcdHandler};
use libfuzzer_sys::fuzz_target;
use rustc_hash::FxHashSet;

#[derive(Default)]
struct Sink;

impl VcdHandler for Sink {
    fn on_timestamp(&mut self, _time: u64) -> ControlFlow<()> {
        ControlFlow::Continue(())
    }

    fn on_time_update(&mut self, _time: u64, _byte_offset: u64) {}

    fn on_scalar(&mut self, _id: &str, _value: u8) {}

    fn on_vector(&mut self, _id: &str, _bits: &str) {}
}

fuzz_target!(|data: &[u8]| {
    let mut reader = BufReader::new(Cursor::new(data));
    let Ok(header) = try_parse_header(&mut reader) else {
        return;
    };
    let watched_ids: FxHashSet<String> = header.id_to_indices.into_keys().collect();
    parse_streaming_with_offsets(&mut reader, &watched_ids, &mut Sink, header.body_offset);
});
