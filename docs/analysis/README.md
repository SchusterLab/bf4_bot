# Compressor cooling-water temperature analysis

Backs the `T_CompressorWaterOut` high-temperature alarm (issue #26).

## Figure

`outlet_water_temp_histogram.png` -- distribution of the compressor **outlet**
water temperature (`cpatempwo`, in C) over ~1 month, so we don't have to
re-pull it to remember what "normal" looks like.

## What's normal (30 days, ~06-15 to 07-15 2026, n ~ 132k @ ~20 s cadence)

| series | field | mean | median | p99.9 | max |
|---|---|---|---|---|---|
| outlet water | `cpatempwo` | 27.71 | 27.67 | 28.89 | 40.28 |
| inlet water  | `cpatempwi` | 18.43 | 18.38 | 19.35 | 21.47 |
| oil          | `cpatempo`  | 26.80 | 26.76 | 27.73 | 34.93 |
| helium       | `cpatemph`  | 77.84 | 77.86 | 78.58 | 78.83 |

Outlet water sits in a very tight band: core **27-28.5 C** (std 0.33 C), a
sharp cliff at 28.5 C, then a nearly-empty tail. Only 0.09% of the month was
above 29 C; 0.03% above 30 C. The `max=40.28` is a single ~1-minute transient
(2026-07-13 ~08:05), not steady state.

## Alarm threshold

Chosen: **> 35 C** (`config/alarms.yaml`, alarm `CompressorWaterOut_high`).
~6.5 C above the normal ceiling, so no nuisance firing; catches a genuine
cooling-water problem well before the compressor's own protection trips.
Runtime-configurable (edit `config/alarms.yaml` or the alarm editor GUI).
For reference, the old lab bot used a fixed 40 C
(`reference/Slackbot/bfg_slackbot.py`, `abort_temps`) -- not a Cryomech spec.

## Regenerate

```
python scripts/analyze_compressor_water_temp.py [DAYS]   # default 30
```

Read-only: reads `Status_*.log` files only; no API calls, no fridge writes.
Overwrites the PNG with a fresh window (window is printed in the figure title).
