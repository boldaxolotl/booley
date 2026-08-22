//! Differential coverage for the timestamp-chunked parallel VCD builder.

use std::fs;
use std::io::BufReader;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

use bwave::cache::ColumnCache;
use bwave::fst::FstBuildHandler;
use bwave::parser::parse_header;

static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

fn fixture(name: &str) -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures")
        .join(name)
}

fn build(input: &Path, parallel: bool, suffix: &str) -> PathBuf {
    let number = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
    let output = std::env::temp_dir().join(format!(
        "bwave_parallel_diff_{}_{}_{}.fst",
        std::process::id(),
        number,
        suffix
    ));
    let file = fs::File::open(input).unwrap();
    let mut reader = BufReader::new(file);
    let header = parse_header(&mut reader);
    let mut handler = FstBuildHandler::new(&header, None, &output).unwrap();
    if parallel {
        // Deliberately tiny targets exercise many boundaries and queue wraps.
        handler
            .parse_bytes_parallel(&mut reader, None, 3, 2, 31, 89)
            .unwrap();
    } else {
        handler.parse_bytes(&mut reader, None).unwrap();
    }
    handler.finalize_and_write().unwrap();
    output
}

fn assert_semantically_equal(serial: &ColumnCache, parallel: &ColumnCache, fixture: &str) {
    assert_eq!(serial.sim_start_tick, parallel.sim_start_tick, "{fixture}");
    assert_eq!(serial.sim_end_tick, parallel.sim_end_tick, "{fixture}");
    assert_eq!(serial.ticks_to_ns, parallel.ticks_to_ns, "{fixture}");
    assert_eq!(serial.timescale_str, parallel.timescale_str, "{fixture}");
    assert_eq!(
        serial.clock_period_ticks, parallel.clock_period_ticks,
        "{fixture}"
    );
    assert_eq!(
        serial.first_rise_tick, parallel.first_rise_tick,
        "{fixture}"
    );
    assert_eq!(serial.clock_id, parallel.clock_id, "{fixture}");
    assert_eq!(
        serial.clock_before_reset_at_deassert, parallel.clock_before_reset_at_deassert,
        "{fixture}"
    );
    assert_eq!(
        serial.clock_table.len(),
        parallel.clock_table.len(),
        "{fixture}"
    );
    for (left, right) in serial.clock_table.iter().zip(&parallel.clock_table) {
        assert_eq!(left.period, right.period, "{fixture}");
        assert_eq!(left.first_rise, right.first_rise, "{fixture}");
        assert_eq!(left.id, right.id, "{fixture}");
    }
    assert_eq!(serial.signals.len(), parallel.signals.len(), "{fixture}");
    for (index, (left, right)) in serial.signals.iter().zip(&parallel.signals).enumerate() {
        assert_eq!(left.name, right.name, "{fixture}, signal {index}");
        assert_eq!(left.width, right.width, "{fixture}, signal {index}");
        assert_eq!(left.var_type, right.var_type, "{fixture}, signal {index}");
        assert_eq!(
            serial.read_transitions(index),
            parallel.read_transitions(index),
            "{fixture}, signal {}",
            left.name
        );
    }
}

#[test]
fn fixture_matrix_matches_serial_and_parallel_output_is_deterministic() {
    let fixtures = [
        "small_clocked.vcd",
        "test_aliases.vcd",
        "test_dumpvars.vcd",
        "test_multiline_var.vcd",
        "test_real_values.vcd",
        "test_unpacked_array.vcd",
        "test_verilator_quirks.vcd",
        "test_wide_signals.vcd",
        "test_wide_xz_outside_slice.vcd",
        "test_xcelium_dialect.vcd",
        "test_xz.vcd",
    ];

    for name in fixtures {
        let input = fixture(name);
        let serial_path = build(&input, false, "serial");
        let parallel_a_path = build(&input, true, "parallel_a");
        let parallel_b_path = build(&input, true, "parallel_b");

        let serial = ColumnCache::load_from_file(&serial_path).unwrap();
        let parallel = ColumnCache::load_from_file(&parallel_a_path).unwrap();
        assert_semantically_equal(&serial, &parallel, name);
        assert_eq!(
            fs::read(&parallel_a_path).unwrap(),
            fs::read(&parallel_b_path).unwrap(),
            "parallel output changed between identical builds of {name}"
        );

        drop(serial);
        drop(parallel);
        for path in [serial_path, parallel_a_path, parallel_b_path] {
            let _ = fs::remove_file(path);
        }
    }
}
