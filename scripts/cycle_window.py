#!/usr/bin/env python3
"""Run the live cycle only while the competition is still open.

`competition_window` closes the front door: the competition account is
mechanically untradeable before kickoff. Nothing closed the back door. The
gate set has no upper bound, `strategy.engine.is_final_date` is an exact
equality test on `final_trading_date`, and the crontab is `* * 1-5` with no
end date — so from the next weekday after the final date the agent would have
resumed opening positions on the very account the judges are reading.

This guard is deliberately NOT a gate: the sixteen BLOCKING gates are a fixed
set the submission names, and a scheduling bound is not an operational
dimension of a trade. It sits in front of the cycle instead, and takes its
date from the manifest so there is no second copy to drift.

Past the final date it hands off with `--exits-only` rather than refusing to
run. Suppressing NEW exposure is the entire requirement; refusing the cycle
would also kill `manage_exits`, which is this repo's only exit path — a
residual position from an unfilled 09-04 flatten limit could then never be
closed through the schedule, and cron would log success while it sat there.
"""

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def past_final_date(today: date, final: date) -> bool:
    """True once the competition is over and the account must be left alone."""
    return today > final


def main() -> int:
    # Through the same loader every other script uses, and with no code-side
    # default for either value: this is the one process cron invokes, in a
    # repo whose README documents a timezone misconfiguration that silently
    # mis-fired the whole schedule. A missing key should be loud, not guessed.
    from policy import loader
    manifest = loader.load()
    final = date.fromisoformat(str(manifest.get("session", "final_trading_date")))
    tz = ZoneInfo(str(manifest.get("session", "timezone")))
    today = datetime.now(timezone.utc).astimezone(tz).date()

    passthrough = sys.argv[1:]
    if past_final_date(today, final):
        sys.stderr.write(
            f"cycle_window: {today} is past the final trading date {final}; "
            f"managing the book only, sizing nothing new. Remove the cron "
            f"entries once the account is flat.\n")
        if "--exits-only" not in passthrough:
            passthrough = passthrough + ["--exits-only"]

    sys.argv = [str(ROOT / "scripts" / "run_cycle.py")] + passthrough
    from scripts.run_cycle import main as run_cycle_main
    return run_cycle_main()


if __name__ == "__main__":
    raise SystemExit(main())
