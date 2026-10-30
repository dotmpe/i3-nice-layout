## Quick start (development)

```bash
cd ~/project/i3-nice-layout
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# run the daemon
i3-nice-layout daemon

# in another terminal
i3-nice-layout pool list
i3-nice-layout rollup toggle
```

Add to i3 config:

```
exec_always --no-startup-id i3-nice-layout daemon
```
