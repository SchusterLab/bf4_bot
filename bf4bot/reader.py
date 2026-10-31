"""
READ-ONLY data access for the BF4 Bluefors fridge.

Two data sources:
1. Log files on S:/_BlueFors4/Logs/ (historical CSV data)
2. Bluefors REST API at https://localhost:49098 (real-time values)

SAFETY: All file access is open(file, "r"). All API calls are HTTP GET.
"""

import logging

import requests
from datetime import date, datetime, time, timedelta

from . import format_duration

requests.packages.urllib3.disable_warnings()

# Logger for parse-error warnings.  A single corrupted line in a
# multi-megabyte log file should skip that line, log why, and carry on
# -- NEVER crash the whole report (issue #3).  Uses the bf4bot logger
# configured by bf4bot.logging_setup in run_bot.py / watchdog.py.
logger = logging.getLogger("bf4bot")

# Default paths -- can be overridden
DEFAULT_LOG_PREFIX = "S:/_BlueFors4/Logs/"
DEFAULT_API_HOST = "https://localhost"
DEFAULT_API_PORT = "49098"


class FridgeReader:
    """Read-only interface to the BF4 fridge data."""

    def __init__(self, api_key=None, api_key_file="api_key.txt", log_prefix=DEFAULT_LOG_PREFIX,
                 api_host=DEFAULT_API_HOST, api_port=DEFAULT_API_PORT):
        self.log_prefix = log_prefix
        self.api_host = api_host
        self.api_port = api_port
        if api_key:
            self.api_key = api_key
        else:
            self.api_key = open(api_key_file).read().strip()

    # ----------------------------------------------------------------
    # Real-time API reads
    # ----------------------------------------------------------------

    def api_read(self, path):
        """Read a single value from the Bluefors REST API (HTTP GET only)."""
        url = f"{self.api_host}:{self.api_port}/values/mapper/{path}"
        # timeout=(connect, read): 5s to establish TCP connection, 10s to
        # receive response.  Without this, a frozen Bluefors API hangs
        # the alarm thread FOREVER -- no alarms, no heartbeat, bot looks dead.
        response = requests.get(url, params={"key": self.api_key}, verify=False,
                                timeout=(5, 10))
        response.raise_for_status()
        data = response.json()
        node_key = f"mapper.{path.replace('/', '.')}"
        content = data["data"][node_key]["content"]
        raw_value = content["latest_value"]["value"]
        status = content["latest_value"]["status"]
        read_only = content["read_only"]
        return {"value": raw_value, "status": status, "read_only": read_only}

    def get_temperature(self, stage):
        """Get real-time temperature for a fridge stage. Returns (value_mK, status)."""
        paths = {
            "MXC": "bf/temperatures/tmixing",
            "Still": "bf/temperatures/tstill",
            "4K": "bf/temperatures/t4k",
            "50K": "bf/temperatures/t50k",
        }
        result = self.api_read(paths[stage])
        if result["status"] == "INVALID" or result["value"] == "":
            return None, result["status"]
        temp_k = float(result["value"])
        if stage in ["MXC", "Still"]:
            return temp_k * 1e3, result["status"]  # mK
        return temp_k, result["status"]  # K

    def get_pressure(self, channel):
        """Get real-time pressure in mbar. channel is 'P1' through 'P6'."""
        path = f"bf/pressures/{channel.lower()}"
        result = self.api_read(path)
        if result["status"] == "INVALID" or result["value"] == "":
            return None, result["status"]
        return float(result["value"]) * 1e3, result["status"]  # bar -> mbar

    def get_flow(self):
        """Get real-time flow rate in mmol/s."""
        result = self.api_read("bf/flow")
        if result["status"] == "INVALID" or result["value"] == "":
            return None, result["status"]
        return float(result["value"]), result["status"]

    def get_compressor_state(self):
        """Get compressor running state. Returns (running_bool_or_None, status).

        Uses explicit ``value in (None, "")`` rather than truthiness,
        because ``if 0:`` is falsy -- the old code returned None
        (reported as "unknown") when the compressor was simply OFF
        (value=0).  Also guards on ``status == "INVALID"`` so a stale
        cached value is not reported as a real reading.  Safety-
        adjacent: a missed compressor-OFF would fool the alarm system.
        """
        result = self.api_read("bflegacy/double/cparun")
        if result["status"] == "INVALID" or result["value"] in (None, ""):
            return None, result["status"]
        return bool(float(result["value"])), result["status"]

    def get_pulse_tube(self):
        """Get pulse tube state. Returns (running_bool_or_None, status).

        Same robust pattern as ``get_compressor_state``.
        """
        result = self.api_read("bf/pulsetube")
        if result["status"] == "INVALID" or result["value"] in (None, ""):
            return None, result["status"]
        return bool(float(result["value"])), result["status"]

    def get_compressor_water_out(self):
        """Get real-time compressor OUTLET (cooling-water return) temperature.

        Returns (value_C, status).  The bflegacy API reports degrees Celsius
        directly -- no scaling.  Backs the T_CompressorWaterOut alarm (issue
        #26), which warns on cooling-water problems before the compressor
        overheats.  Same INVALID/empty guard as the other scalar reads.
        """
        result = self.api_read("bflegacy/double/cpatempwo")
        if result["status"] == "INVALID" or result["value"] == "":
            return None, result["status"]
        return float(result["value"]), result["status"]

    # ----------------------------------------------------------------
    # Turbo pumps (Pfeiffer TC400 controller; typically two per system)
    # ----------------------------------------------------------------
    # All turbo endpoints follow the pattern ``bflegacy/double/tc400<key>``
    # and ``bflegacy/double/tc400<key>_2`` for the second pump, verified
    # against the Bluefors Control Software UI (see scripts/probe_turbo_api.py).
    #
    # idx=1 -> primary turbo, idx=2 -> auxiliary/second turbo.

    def _turbo_key(self, base, idx):
        """Build the correct tc400 API key for a given pump index (1 or 2)."""
        suffix = "" if idx == 1 else "_2"
        return f"bflegacy/double/tc400{base}{suffix}"

    def get_turbo_state(self, idx):
        """Return (running_bool, status) for turbo pump idx (1 or 2).

        ``tc400pumpstatn`` is 1.0 when the pump is running, 0.0 when off.
        """
        result = self.api_read(self._turbo_key("pumpstatn", idx))
        if result["status"] == "INVALID" or result["value"] in (None, ""):
            return None, result["status"]
        return bool(float(result["value"])), result["status"]

    def get_turbo_ovtemp(self, idx):
        """Return (overtemp_flag, status) for turbo pump idx.

        True means the pump body OR the electronics reports too-hot.  We
        OR the two underlying flags (``ovtemppum``, ``ovtempelec``) so a
        single variable captures either failure mode.
        """
        r_pump = self.api_read(self._turbo_key("ovtemppum", idx))
        r_elec = self.api_read(self._turbo_key("ovtempelec", idx))
        # If either call failed we still return what we can -- an OR of
        # two booleans where one side is None treats None as False.
        p_val = r_pump.get("value")
        e_val = r_elec.get("value")
        if p_val in (None, "") and e_val in (None, ""):
            return None, r_pump.get("status", "INVALID")
        over = bool(float(p_val) if p_val not in (None, "") else 0) or \
               bool(float(e_val) if e_val not in (None, "") else 0)
        return over, r_pump.get("status", "SYNCHRONIZED")

    def get_all_live(self):
        """Get a snapshot of all key live values. Returns a dict."""
        snapshot = {}

        for stage in ["MXC", "Still", "4K", "50K"]:
            val, status = self.get_temperature(stage)
            unit = "mK" if stage in ["MXC", "Still"] else "K"
            snapshot[f"T_{stage}"] = {"value": val, "unit": unit, "status": status}

        for p in ["P1", "P2", "P3", "P4", "P5", "P6"]:
            val, status = self.get_pressure(p)
            snapshot[p] = {"value": val, "unit": "mbar", "status": status}

        val, status = self.get_flow()
        snapshot["Flow"] = {"value": val, "unit": "mmol/s", "status": status}

        val, status = self.get_compressor_state()
        snapshot["Compressor"] = {"value": val, "unit": "", "status": status}

        val, status = self.get_pulse_tube()
        snapshot["PulseTube"] = {"value": val, "unit": "", "status": status}

        # Compressor cooling-water OUTLET temperature (issue #26), in C.
        val, status = self.get_compressor_water_out()
        snapshot["T_CompressorWaterOut"] = {"value": val, "unit": "C", "status": status}

        # Turbo pumps -- two by default.  "Turbo<i>" is the running bool,
        # "Turbo<i>_ovtemp" is a combined pump-or-electronics overtemp flag.
        # The alarm editor exposes all four as boolean-alarmable variables.
        for idx in (1, 2):
            val, status = self.get_turbo_state(idx)
            snapshot[f"Turbo{idx}"] = {"value": val, "unit": "", "status": status}
            val, status = self.get_turbo_ovtemp(idx)
            snapshot[f"Turbo{idx}_ovtemp"] = {"value": val, "unit": "", "status": status}

        return snapshot

    # ----------------------------------------------------------------
    # Historical log file reads
    # ----------------------------------------------------------------

    def _parse_log_date(self, date_str):
        """Parse DD-MM-YY date string from log files."""
        day = int(date_str[0:2])
        month = int(date_str[3:5])
        year = int(date_str[6:8]) + 2000
        return date(year, month, day)

    def read_temperature_log(self, target_date, channel):
        """Read temperature log for a single day. Returns (timestamps, values_in_kelvin)."""
        date_str = target_date.strftime("%y-%m-%d")
        filename = f"{self.log_prefix}{date_str}/CH{channel} T {date_str}.log"
        timestamps, values = [], []
        try:
            with open(filename, "r") as f:
                for lineno, line in enumerate(f, start=1):
                    try:
                        parts = line.strip().split(",")
                        if len(parts) >= 3:
                            # Parse ALL fields before appending, so a
                            # failure on the value doesn't leave
                            # timestamps ahead of values.
                            d = self._parse_log_date(parts[0].strip())
                            t = time.fromisoformat(parts[1])
                            ts_new = datetime.combine(d, t)
                            val_new = float(parts[2])
                            timestamps.append(ts_new)
                            values.append(val_new)
                    except (ValueError, IndexError) as e:
                        logger.warning(
                            "Skipping bad line %d in %s: %s",
                            lineno, filename, e,
                        )
        except FileNotFoundError:
            # Log files for a given day may simply not exist yet; silent skip.
            pass
        except OSError as e:
            # PermissionError, drive disconnect (S: mapped offline),
            # I/O errors mid-read -- return partial data, do NOT crash.
            logger.warning("Could not read %s: %s", filename, e)
        return timestamps, values

    def read_pressure_log(self, target_date):
        """Read all 6 pressure channels for a single day. Returns (timestamps, [[P1..P6]])."""
        date_str = target_date.strftime("%y-%m-%d")
        filename = f"{self.log_prefix}{date_str}/maxigauge {date_str}.log"
        timestamps, pressures = [], []
        try:
            with open(filename, "r") as f:
                for lineno, line in enumerate(f, start=1):
                    try:
                        parts = line.strip().split(",")
                        if len(parts) > 35:
                            # Build BOTH the timestamp and the pressure
                            # row before appending either, so a float
                            # failure does not leave the lists misaligned.
                            d = self._parse_log_date(parts[0].strip())
                            t = time.fromisoformat(parts[1])
                            ts_new = datetime.combine(d, t)
                            row = [
                                float(parts[5]), float(parts[11]), float(parts[17]),
                                float(parts[23]), float(parts[29]), float(parts[35]),
                            ]
                            timestamps.append(ts_new)
                            pressures.append(row)
                    except (ValueError, IndexError) as e:
                        logger.warning(
                            "Skipping bad line %d in %s: %s",
                            lineno, filename, e,
                        )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not read %s: %s", filename, e)
        return timestamps, pressures

    def read_flowmeter_log(self, target_date):
        """Read flowmeter log for a single day. Returns (timestamps, values)."""
        date_str = target_date.strftime("%y-%m-%d")
        filename = f"{self.log_prefix}{date_str}/Flowmeter {date_str}.log"
        timestamps, values = [], []
        try:
            with open(filename, "r") as f:
                for lineno, line in enumerate(f, start=1):
                    try:
                        parts = line.strip().split(",")
                        if len(parts) >= 3:
                            # Parse ALL fields before appending (see
                            # read_temperature_log for why).
                            d = self._parse_log_date(parts[0].strip())
                            t = time.fromisoformat(parts[1])
                            ts_new = datetime.combine(d, t)
                            val_new = float(parts[2])
                            timestamps.append(ts_new)
                            values.append(val_new)
                    except (ValueError, IndexError) as e:
                        logger.warning(
                            "Skipping bad line %d in %s: %s",
                            lineno, filename, e,
                        )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not read %s: %s", filename, e)
        return timestamps, values

    def read_status_log(self, target_date, keys):
        """Read key-value pairs from Status log. Returns {key: (timestamps, values)}."""
        date_str = target_date.strftime("%y-%m-%d")
        filename = f"{self.log_prefix}{date_str}/Status_{date_str}.log"
        result = {k: ([], []) for k in keys}
        try:
            with open(filename, "r") as f:
                for lineno, line in enumerate(f, start=1):
                    try:
                        parts = line.strip().split(",")
                        if len(parts) < 4:
                            continue
                        d = self._parse_log_date(parts[0].strip())
                        t = time.fromisoformat(parts[1])
                        ts = datetime.combine(d, t)
                        for i in range(2, len(parts) - 1, 2):
                            key = parts[i].strip()
                            if key in result:
                                try:
                                    result[key][0].append(ts)
                                    result[key][1].append(float(parts[i + 1]))
                                except (ValueError, IndexError):
                                    pass
                    except (ValueError, IndexError) as e:
                        logger.warning(
                            "Skipping bad line %d in %s: %s",
                            lineno, filename, e,
                        )
        except FileNotFoundError:
            pass
        except OSError as e:
            logger.warning("Could not read %s: %s", filename, e)
        return result

    def read_history(self, hours=24, end=None, anchor=None):
        """Read all sensor data for a window of ``hours`` hours.

        By default the window ENDS at datetime.now() -- the usual
        "last N hours" report.  Pass ``end`` (a datetime) to anchor the
        window's end at a fixed time instead; data is then filtered to
        ``cutoff <= t <= end``.

        BOTH bounds matter.  With a past ``end`` the day's log file also
        holds samples recorded AFTER the window ends, which must be
        excluded -- the old code filtered only ``t >= cutoff`` because the
        window always ended at "now" and logs never contain future data.

        ``anchor`` is optional metadata (the user-supplied center time for
        an at-time report).  It does NOT affect which samples are selected
        -- the caller computes ``end`` from it -- but it is carried through
        in the returned dict so plot titles can say "around <anchor>"
        instead of "last N hours".  Returns a dict of everything.
        """
        now = end if end is not None else datetime.now()
        cutoff = now - timedelta(hours=hours)
        start_date = cutoff.date()
        end_date = now.date()

        def collect(reader_func, *args):
            all_ts, all_vals = [], []
            for i in range((end_date - start_date).days + 1):
                day = start_date + timedelta(days=i)
                ts, vals = reader_func(day, *args)
                all_ts.extend(ts)
                all_vals.extend(vals)
            # Filter to the [cutoff, now] window -- BOTH bounds (a
            # past-anchored window must drop same-day samples after `now`).
            mask = [cutoff <= t <= now for t in all_ts]
            return (
                [t for t, m in zip(all_ts, mask) if m],
                [v for v, m in zip(all_vals, mask) if m],
            )

        def collect_status(keys):
            combined = {k: ([], []) for k in keys}
            for i in range((end_date - start_date).days + 1):
                day = start_date + timedelta(days=i)
                day_data = self.read_status_log(day, keys)
                for k in keys:
                    combined[k][0].extend(day_data[k][0])
                    combined[k][1].extend(day_data[k][1])
            # Filter to the [cutoff, now] window (both bounds, as above).
            for k in keys:
                mask = [cutoff <= t <= now for t in combined[k][0]]
                combined[k] = (
                    [t for t, m in zip(combined[k][0], mask) if m],
                    [v for v, m in zip(combined[k][1], mask) if m],
                )
            return combined

        history = {
            "cutoff": cutoff,
            "now": now,
            "hours": hours,
            "label": format_duration(hours),
            "anchor": anchor,
            "temps": {},
            "pressures": {"timestamps": [], "values": []},
            "flow": {"timestamps": [], "values": []},
            "compressor": {},
        }

        for name, ch in [("Still", 5), ("50K", 1), ("MXC", 6), ("4K", 2)]:
            ts, vals = collect(self.read_temperature_log, ch)
            history["temps"][name] = {"timestamps": ts, "values": vals}

        ts, vals = collect(self.read_pressure_log)
        history["pressures"] = {"timestamps": ts, "values": vals}

        ts, vals = collect(self.read_flowmeter_log)
        history["flow"] = {"timestamps": ts, "values": vals}

        # All compressor series the compressor plot needs, read in one pass.
        # The pressure/current keys (cpalp/cpahp/cpacurrent) used to be bolted
        # on separately inside bot.send_compressor_report; that left the
        # @mention path -- which calls read_history + plot_compressor directly
        # -- with blank pressure/current panels (issue #24).  Populating them
        # here makes read_history the single source of compressor data.
        comp_keys = ["cpatempwi", "cpatempwo", "cpatempo", "cpatemph",
                     "cpalp", "cpahp", "cpacurrent", "cpahours"]
        history["compressor"] = collect_status(comp_keys)

        turbo_keys = [
            "tc400actualspd", "tc400drvpower", "tc400ovtempelec", "tc400ovtemppum",
            "tc400pumpstatn", "tc400standby",
            "tc400actualspd_2", "tc400drvpower_2", "tc400ovtempelec_2", "tc400ovtemppum_2",
            "tc400pumpstatn_2", "tc400standby_2",
        ]
        history["turbo"] = collect_status(turbo_keys)

        return history
