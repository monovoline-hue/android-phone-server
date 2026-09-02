# Mi10Pro Monitor (cloud side)

Pulls `~/zonira-monitor/current.json` from the Xiaomi Mi 10 Pro over
Tailscale SSH every 60s, evaluates alert rules, and renders a static
dashboard. The phone only reports; all judgement happens here.

## Layout

    dashboard/fetch.py         fetch -> judge -> render (one shot)
    dashboard/run_loop.py      unprivileged 60s loop (fallback driver)
    dashboard/prune_history.py rolling retention for history.jsonl
    dashboard/rules.json       alert thresholds (edit freely)
    dashboard/template.html    dashboard template
    dashboard/data/            history.jsonl, current.json, series.json, state.json
    dashboard/out/             dashboard.html, index.html -> dashboard.html
    tests/                     non-destructive regression suites
    logs/                      fetch.log, web.log, prune.log

## Units

    mi10pro-fetch.timer     every 60s  (OnBootSec=30s, OnUnitActiveSec=60s)
    mi10pro-fetch.service   oneshot, runs fetch.py
    mi10pro-web.service     python3 -m http.server 8080, Restart=always
    mi10pro-prune.timer     daily 03:30, keeps MI10PRO_KEEP_DAYS (30)

## Common commands

    systemctl list-timers "mi10pro-*"         # scheduler state
    journalctl -u mi10pro-fetch.service -n 50
    tail -f /opt/mi10pro-monitor/logs/fetch.log
    curl -s http://127.0.0.1:8080/ | head     # dashboard reachable?

    # alert rules: 19 cases, constructed data, touches nothing
    python3 /opt/mi10pro-monitor/tests/regress_rules.py
    # history pruning: synthetic data in a temp dir
    python3 /opt/mi10pro-monitor/tests/test_prune.py

## Notes

- Sampling cadence lives in TWO places that must agree:
  the timer (OnUnitActiveSec) and MI10PRO_POLL_SECONDS (used to convert the
  "N minutes offline" rule into a failure count). Changing one without the
  other silently skews the offline window.
- Battery level/status come from an ADB supplement that only exists on the
  workstation. On this host those fields are null and labelled as such -
  they are never invented.
- Tailscale must be authorised on THIS node separately from any other device.
  `sudo tailscale up --hostname=zonira-cloud` prints a login URL.
