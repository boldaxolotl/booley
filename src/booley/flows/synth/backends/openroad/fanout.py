"""Native fanout provenance and boundary safety for physical synthesis.

The private complete rounded report proves integer/load safety only. Exact public
values come from the consecutive native minimum-slack getter, never its formatter.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BLOCKED = (
    "sta::clear_network sta::clear_network_corner_base sta::make_sta "
    "sta::delete_all_memory sta::init_sta sta::network_changed sta::link_design_cmd "
    "ord::read_verilog_cmd ord::link_design_db_cmd ord::read_db_cmd ord::read_def_cmd "
    "ord::read_lef_cmd ord::read_3dbv_cmd ord::read_3dbx_cmd ord::read_3dblox_bmap_cmd "
    "ord::set_cmd_sta ord::design_created odb::read_db odb::read_def odb::read_lef "
    "odb::dbDatabase_read odb::dbDatabase_readEco odb::dbDatabase_triggerPostReadDb "
    "odb::dbDatabase_clear odb::dbDatabase_destroy odb::dbBlock_clear odb::dbBlock_destroy "
    "odb::dbChip_destroy odb::dbMaster_destroy odb::dbLib_destroy odb::dbTech_destroy "
    "odb::dbBTerm_destroy odb::dbModule_destroy odb::dbModBTerm_destroy "
    "odb::dbChipInst_destroy odb::dbModInst_destroy"
)


def policy_tcl(report_dir: Path, configured: int | None) -> str:
    """Initialize observers after linking and before any authored SDC."""
    return (
        _POLICY.replace("@BLOCKED@", _BLOCKED)
        + f"\nset ::booley_fanout::report_dir {{{report_dir.as_posix()}}}\n"
        + f"set ::booley_fanout::configured {{{configured if configured is not None else ''}}}\n"
        + "booley_fanout::initialize\n"
    )


_POLICY = r"""
namespace eval booley_fanout {
  variable rules [dict create]
  variable unsupported 0
  variable transitioned 0
  variable phase generated
  variable source booley_default
  variable observers {}
  variable blocked {@BLOCKED@}
}
proc booley_fanout::fail {message} {
  error "BOOLEY_INPUT_ERROR: unsupported exact fanout evidence: $message"
}
proc booley_fanout::reject {command op} {
  variable unsupported
  set unsupported 1
  fail "attempted destructive native context command [lindex $command 0]"
}
proc booley_fanout::observe {leaf command code result op} {
  variable unsupported
  variable phase
  variable source
  variable rules
  variable transitioned
  if {$code != 0 || $phase ni {authored assignment}} {
    set unsupported 1
    return
  }
  set leaf [namespace tail $leaf]
  if {$leaf eq "clear_sta_corner_base"} {
    set rules [dict create]
    if {[info exists ::sta::corner_bundle_applied]} {
      set ::sta::corner_bundle_applied [dict create]
    }
  } elseif {$leaf eq "set_cmd_mode"} {
    if {[sta::cmd_mode_name] ne "default"} {set transitioned 1}
  } else {
    if {[lindex $command 2] ne "max"} {return}
    binary scan [binary format f [lindex $command 3]] f native
    set authority $source
    if {$phase eq "authored"} {set authority sdc}
    dict set rules [lindex $command 1] [list $native $authority]
  }
}
proc booley_fanout::inventory {{record 0}} {
  variable blocked
  foreach leaf [concat $blocked {
    sta::set_cell_fanout_limit sta::set_port_fanout_limit
    sta::clear_sta_corner_base sta::set_cmd_mode sta::clear_sta
  }] {
    if {[llength [info commands ::$leaf]] != 1} {fail "missing native command $leaf"}
    set public [expr {$leaf in {sta::clear_sta sta::clear_network}}]
    if {([llength [info procs ::$leaf]] != 0) != $public} {
      fail "unexpected native/procedure command type $leaf"
    }
    if {$record} {puts "OPENROAD_FANOUT_INVENTORY: $leaf [expr {$public ? {procedure} : {native}}]"}
  }
  foreach leaf {sta::read_verilog_cmd sta::remove_constraints} {
    if {[llength [info commands ::$leaf]]} {fail "unexpected command $leaf"}
    if {$record} {puts "OPENROAD_FANOUT_INVENTORY: $leaf ABSENT"}
  }
}
proc booley_fanout::mode_guard {} {
  inventory
  variable initial_mode
  variable unsupported
  variable transitioned
  if {$unsupported || $transitioned || [sta::cmd_mode_name] ne "default" ||
      [sta::cmd_mode] ne $initial_mode || [get_modes *] ne [list $initial_mode]} {
    fail "requires a single unchanged default native mode and intact context"
  }
  set scenes [lsort -unique [get_scenes *]]
  if {![llength $scenes] || $scenes ne [lsort -unique [get_scenes -modes default *]]} {
    fail "scene reporting modes do not match the singleton repair mode"
  }
  variable observers
  foreach observer $observers {
    lassign $observer leaf event callback
    if {[lsearch -exact [trace info execution $leaf] [list $event $callback]] < 0} {
      fail "lost native observer $leaf"
    }
  }
}
proc booley_fanout::cleanup {} {
  variable observers
  foreach observer $observers {
    lassign $observer leaf event callback
    catch {trace remove execution $leaf $event $callback}
  }
  set observers {}
}
proc booley_fanout::assign {value authority} {
  variable phase
  variable source
  set phase assignment
  set source $authority
  set code [catch {set_max_fanout $value [current_design]} result options]
  set phase generated
  if {$code} {return -options $options $result}
}
proc booley_fanout::initialize {} {
  variable initial_mode
  variable blocked
  variable observers
  variable phase
  variable source
  inventory 1
  set initial_mode [sta::cmd_mode]
  mode_guard
  foreach leaf $blocked {
    trace add execution $leaf enter booley_fanout::reject
    lappend observers [list $leaf enter booley_fanout::reject]
  }
  foreach leaf {sta::set_cell_fanout_limit sta::set_port_fanout_limit
                sta::clear_sta_corner_base sta::set_cmd_mode} {
    set callback [list booley_fanout::observe $leaf]
    trace add execution $leaf leave $callback
    lappend observers [list $leaf leave $callback]
  }
  set phase assignment
  set source booley_default
  set_max_fanout 1e20 [current_design]
  set phase authored
}
proc booley_fanout::authored_done {} {
  variable phase
  set phase generated
  mode_guard
}
proc booley_fanout::final_rule {} {
  variable rules
  variable configured
  set top [sta::Instance_cell [sta::top_instance]]
  if {![dict exists $rules $top] || [lindex [dict get $rules $top] 1] ne "sdc"} {
    assign 1e20 booley_default
  }
  set authored 0
  dict for {object rule} $rules {
    if {[lindex $rule 1] eq "sdc"} {set authored 1}
  }
  if {!$authored && $configured ne ""} {assign $configured setting}
  return $authored
}
proc booley_fanout::capacity {} {
  set count 0
  set instances [list [sta::top_instance]]
  set iterator [sta::leaf_instance_iterator]
  while {[sta::LeafInstanceIterator_has_next $iterator]} {
    lappend instances [sta::LeafInstanceIterator_next $iterator]
  }
  sta::LeafInstanceIterator_finish $iterator
  foreach instance $instances {
    set iterator [sta::Instance_pin_iterator $instance]
    while {[sta::InstancePinIterator_has_next $iterator]} {
      sta::InstancePinIterator_next $iterator
      incr count
    }
    sta::InstancePinIterator_finish $iterator
  }
  return $count
}
proc booley_fanout::literal_pin {name} {
  set pin [sta::find_pin $name]
  if {$pin eq "NULL" || [get_property $pin full_name] ne $name} {
    fail "unresolvable selected native pin $name"
  }
  return $pin
}
proc booley_fanout::read_row {stream first} {
  if {![regexp {^Pin (.+)  $} $first -> name]} {fail "malformed private pin row"}
  if {[gets $stream limit_line] < 0 || [gets $stream fanout_line] < 0 ||
      [gets $stream separator] < 0 || [gets $stream slack_line] < 0} {
    fail "incomplete native fanout row $name"
  }
  set body "$limit_line\n$fanout_line\n$separator\n$slack_line"
  if {![regexp {^max fanout +(-?[0-9]+)\nfanout +(-?[0-9]+)\n-+\nSlack +(-?[0-9]+) \((MET|VIOLATED)\)$} $body -> limit fanout slack status]} {
    fail "malformed native numeric/status row $name"
  }
  return [list $name $limit_line $fanout_line $separator $slack_line $limit $fanout $slack $status]
}
proc booley_fanout::scan {path capacity worst top_default} {
  set stream [open $path r]
  set seen [dict create]
  set violations 0
  set complete 0
  set rows 0
  set header_seen 0
  set cached_body0 ""
  set cached_body1 ""
  set cached_values0 {}
  set cached_values1 {}
  while {[gets $stream line] >= 0} {
    if {$line eq "BOOLEY_FANOUT_COMPLETE"} {set complete 1; break}
    if {$line eq ""} {continue}
    if {$line eq "max fanout"} {
      if {$header_seen || $rows} {fail "repeated or misplaced native report header"}
      set header_seen 1
      continue
    }
    if {![regexp {^Pin (.+)  $} $line -> name]} {fail "malformed private pin row"}
    if {[gets $stream limit_line] < 0 || [gets $stream fanout_line] < 0 ||
        [gets $stream separator] < 0 || [gets $stream slack_line] < 0} {
      fail "incomplete private native row $name"
    }
    set body "$limit_line\n$fanout_line\n$separator\n$slack_line"
    if {$body eq $cached_body0} {
      lassign $cached_values0 limit fanout slack status
    } elseif {$body eq $cached_body1} {
      lassign $cached_values1 limit fanout slack status
    } else {
      if {![regexp {^max fanout +(-?[0-9]+)\nfanout +(-?[0-9]+)\n-+\nSlack +(-?[0-9]+) \((MET|VIOLATED)\)$} $body -> limit fanout slack status]} {
        fail "malformed private native numeric/status row $name"
      }
      set cached_body1 $cached_body0
      set cached_values1 $cached_values0
      set cached_body0 $body
      set cached_values0 [list $limit $fanout $slack $status]
    }
    if {[dict exists $seen $name]} {fail "duplicate native fanout identity $name"}
    dict set seen $name 1
    if {$rows == 0 && [list $name $limit_line $fanout_line $separator $slack_line $limit $fanout $slack $status] ne $worst} {
      fail "compact/private native check mismatch"
    }
    incr rows
    if {$rows > $capacity} {fail "private report exceeds all-pin capacity"}
    if {$status eq "VIOLATED"} {
      incr violations
      if {$limit >= 2147483648} {fail "unsafe violated native fanout limit $limit"}
    }
    if {$top_default && $limit == 100000002004087734272 &&
        2*$fanout+1 >= 200000004008175468544} {
      fail "default load bound unproven for $name"
    }
  }
  if {$complete} {
    while {[gets $stream line] >= 0} {
      if {$line ne ""} {fail "data after private completion marker"}
    }
  }
  close $stream
  if {!$complete || $rows > $capacity ||
      ($rows == 0) != ([llength $worst] == 0) ||
      $violations != [sta::max_fanout_violation_count]} {
    fail "incomplete private native safety evidence"
  }
  puts "OPENROAD_FANOUT_SAFETY: capacity=$capacity rows=$rows violations=$violations complete=$complete"
}
proc booley_fanout::worst_row {path} {
  set stream [open $path r]
  set row {}
  while {[gets $stream line] >= 0} {
    if {$line eq "" || $line eq "max fanout"} {continue}
    if {[llength $row]} {fail "multiple minimum-slack native checks"}
    set row [read_row $stream $line]
  }
  close $stream
  return $row
}
proc booley_fanout::number {value} {
  if {$value != $value || abs($value) >= 1.0000000150474662e30} {
    fail "nonfinite or unrepresentable native max_fanout $value"
  }
  return [format %.17g $value]
}
proc booley_fanout::candidate {pin top} {
  variable rules
  set candidate [dict get $rules $top]
  if {[sta::Pin_is_top_level_port $pin]} {
    set scope [sta::Pin_port $pin]
  } else {set scope [sta::Instance_cell [sta::Pin_instance $pin]]}
  if {[dict exists $rules $scope]} {
    set scoped [dict get $rules $scope]
    if {[lindex $scoped 0] < [lindex $candidate 0]} {set candidate $scoped}
  }
  return $candidate
}
proc booley_fanout::json_string {value} {
  return "\"[string map [list \\ \\\\ \" \\\" \n \\n \r \\r \t \\t] $value]\""
}
proc booley_fanout::checkpoint {} {
  set code [catch {booley_fanout::capture} result options]
  cleanup
  if {$code} {return -options $options $result}
}
proc booley_fanout::capture {} {
  variable report_dir
  variable configured
  variable rules
  mode_guard
  set authored [final_rule]
  mode_guard
  set top [sta::Instance_cell [sta::top_instance]]
  lassign [dict get $rules $top] top_limit top_source
  number $top_limit
  if {$top_limit <= 0} {fail "nonpositive top fanout candidate"}
  set capacity [capacity]
  file mkdir $report_dir
  set worst_path [file join $report_dir worst.rpt]
  report_check_types -max_fanout -verbose -max_count 1 > $worst_path
  set exact [sta::max_fanout_min_slack_limit]
  set slack [sta::max_fanout_min_slack]
  if {$slack != $slack || abs($slack) > 3.4028234663852886e38} {
    fail "nonfinite native fanout slack"
  }
  set row [worst_row $worst_path]
  set status observed
  set worst null
  if {![llength $row]} {
    if {$exact != 1.0000000150474662e30 || $slack != 1.0000000150474662e30} {
      fail "empty report without native no-check limit/slack"
    }
    set status no_applicable_driver
  } else {
    number $exact
    if {$exact <= 0} {fail "nonpositive effective native fanout"}
    set printed [lindex $row 5]
    set printed_slack [lindex $row 7]
    if {$printed ne [format %.0f $exact] || $printed_slack ne [format %.0f $slack]} {
      fail "native getter/rounded report mismatch"
    }
    set name [lindex $row 0]
    set pin [literal_pin $name]
    lassign [candidate $pin $top] limit authority
    if {$exact < $limit} {set authority library} elseif {$exact != $limit} {
      fail "unexplained effective native limit"
    }
    set value [number $exact]
    if {$authority eq "booley_default"} {set value null}
    set worst "\{\"selection\":\"minimum_slack\",\"analysis_mode\":\"default\",\"pin\":[json_string $name],\"max_fanout\":$value,\"source\":[json_string $authority],\"native_check_artifact\":\"worst.rpt\"\}"
  }
  set value [number $top_limit]
  if {$top_source eq "booley_default"} {set value null}
  set setting null
  if {$configured ne ""} {set setting $configured}
  set suppression null
  if {$authored} {set suppression "\"authored_sdc_fanout\""}
  set json "\{\"configured_max_fanout\":$setting,\"suppression_reason\":$suppression,\"design_rule\":\{\"scope\":\"current_design\",\"max_fanout\":$value,\"source\":[json_string $top_source]\},\"status\":[json_string $status],\"worst_check\":$worst\}"
  set stream [open [file join $report_dir policy.json] w]
  puts $stream $json
  close $stream
  puts "OPENROAD_FANOUT_POLICY: $json"
  if {$authored && $configured ne ""} {
    puts "OPENROAD_FANOUT_NOTICE: Target max_fanout suppressed by authored SDC"
  }
  flush stdout
  set private_path [file join $report_dir safety.rpt]
  set count [expr {max(1,$capacity)}]
  report_check_types -max_fanout -verbose -max_count $count > $private_path
  set stream [open $private_path a]
  puts $stream BOOLEY_FANOUT_COMPLETE
  close $stream
  scan $private_path $capacity $row [expr {$top_source eq "booley_default"}]
}
"""


def parse_policy(text: str) -> dict[str, Any]:
    """Read additive native policy metadata without manufacturing missing evidence."""
    import json

    value = json.loads(text)
    if not isinstance(value, dict) or value.get("status") not in {
        "observed",
        "no_applicable_driver",
    }:
        raise ValueError("malformed native fanout policy")
    _check_policy_inputs(value)
    _check_policy_rule(value.get("design_rule"), design=True)
    if value.get("worst_check") is not None:
        _check_policy_rule(value["worst_check"])
    worst = value.get("worst_check")
    if (worst is None) != (value["status"] == "no_applicable_driver"):
        raise ValueError("inconsistent native no-check evidence")
    if worst is not None and (
        worst.get("selection") != "minimum_slack"
        or worst.get("analysis_mode") != "default"
        or not isinstance(worst.get("pin"), str)
        or not worst["pin"]
        or worst.get("native_check_artifact") != "worst.rpt"
    ):
        raise ValueError("malformed selected native fanout check")
    return value


def _check_policy_inputs(value: dict[str, Any]) -> None:
    from booley.flows.synth.ppa_config import validate_max_fanout

    if "configured_max_fanout" not in value or "suppression_reason" not in value:
        raise ValueError("missing native fanout policy inputs")
    configured = value["configured_max_fanout"]
    if configured is not None:
        validate_max_fanout(configured, field="native configured_max_fanout")
    if value["suppression_reason"] not in (None, "authored_sdc_fanout"):
        raise ValueError("malformed native fanout suppression")


def _check_policy_rule(rule: object, *, design: bool = False) -> None:
    import math

    sources = {"sdc", "setting", "booley_default"}
    if not design:
        sources.add("library")
    if not isinstance(rule, dict) or rule.get("source") not in tuple(sources):
        raise ValueError("malformed native fanout authority")
    if design and rule.get("scope") != "current_design":
        raise ValueError("missing native top-design scope")
    limit = rule.get("max_fanout")
    if rule["source"] == "booley_default":
        if limit is not None:
            raise ValueError("default native fanout must have a null public value")
    elif (
        isinstance(limit, bool)
        or not isinstance(limit, (int, float))
        or not (0 < limit < 1.0000000150474662e30 and math.isfinite(limit))
    ):
        raise ValueError("invalid effective native fanout value")
