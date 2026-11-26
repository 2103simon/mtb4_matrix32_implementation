"""Fail-closed E-Matrix32 configuration command/indication exchange.

The manual documents >OK< / >ERR< indications for configuration acceptance.
A successful write-without-response is NOT an acknowledgement. This module
requires exactly a positive indication for the request before returning.

One request at a time; no stimulation commands are sent here. The protocol
does not provide request IDs, so a delayed unrelated >OK< cannot be proven
to belong to the current command: collect device traces during bench QC.
"""
import asyncio
import time


DEFAULT_ACK_TIMEOUT_S = 2.0


class ConfigurationAckError(RuntimeError):
    """A configuration write did not receive an unambiguous positive reply."""

    def __init__(self, record):
        self.record = record
        super().__init__(
            "{} config command acknowledgement {} (response {})".format(
                record["command"], record["status"],
                record.get("response_hex") or "none"))


def config_command_name(payload):
    """Extract e.g. SF from the sent framed command, without decoding binary."""
    data = bytes(payload)
    if not (data.startswith(b">") and data.endswith(b"<") and b";" in data):
        raise ValueError("Malformed configuration command framing")
    field = data[1:data.find(b";")]
    if not (field.isalpha() and 2 <= len(field) <= 3 and field.isupper()):
        raise ValueError("Unexpected Matrix32 configuration command identifier")
    return field.decode("ascii")


def parse_config_ack(payload):
    """Parse bare or *single-NUL-padded* firmware acknowledgement.

    Device E-Matrix32 S/N 0069 observed 2026-10-21:
      SF=1 reply: 3e 4f 4b 3c 00 = b'>OK<\x00'
    Preserve the unmodified payload in response_hex. Reject arbitrary
    trailing bytes, multiple NULs, and non-ACK indications: fail closed.
    """
    raw = bytes(payload)
    if raw in (b">OK<", b">OK<\x00"):
        return "OK"
    if raw in (b">ERR<", b">ERR<\x00"):
        return "ERR"
    raise ValueError("Unknown config indication payload: {!r}".format(raw))


async def write_config_with_ack(client, write_uuid, indicate_uuid, message,
                                timeout_s=DEFAULT_ACK_TIMEOUT_S, record=None,
                                marker=None):
    """Send ONE config command and verify the exact positive firmware indication.

    Always unsubscribes in finally. Throws ConfigurationAckError with a
    machine-readable .record for ERR, timeout, ambiguous/foreign indication,
    GATT failures and failed subscription cleanup. Cancellation propagates.

    A caller may supply a dict to retain partial evidence even if its overall
    operation deadline expires during subscription, writing, or cleanup.
    An optional marker object must expose mark() -> dict with status == "ACK".
    The marker handshake is executed after indication subscription but before
    the BLE write. Marker failure blocks the Matrix32 write fail-closed.
    The monotonic timestamps are host observations, not device apply times.
    """
    if not (0 < timeout_s <= 10):
        raise ValueError("Configuration acknowledgement timeout must be 0..10 s")
    command = config_command_name(message)
    if record is None:
        record = {}
    record.update({
        "command": command, "request_hex": bytes(message).hex(" "),
        "status": "NOT_VERIFIED", "response_hex": None, "elapsed_ms": None,
        "request_monotonic_s": time.monotonic(),
        "subscribe_begin_monotonic_s": None, "subscribe_return_monotonic_s": None,
        "write_begin_monotonic_s": None, "write_return_monotonic_s": None,
        "ack_monotonic_s": None,
        "unsubscribe_begin_monotonic_s": None, "unsubscribe_return_monotonic_s": None,
        "completed_monotonic_s": None,
        "indications": [], "cleanup_status": "NOT_SUBSCRIBED",
        "abort_requested": bool(record.get("abort_requested", False)),
        "marker": None, "marker_error": None,
    })
    if not client.is_connected:
        record["status"] = "BLE_DISCONNECTED"
        raise ConfigurationAckError(record)
    loop = asyncio.get_running_loop()
    response = loop.create_future()
    seen = []
    pending = [False]

    def on_indication(_sender, data):
        raw = bytes(data)
        received = time.monotonic()
        seen.append(raw)
        record["indications"].append({
            "monotonic_s": received, "response_hex": raw.hex(" "),
        })
        if pending[0] and not response.done():
            record["ack_monotonic_s"] = received
            record["response_hex"] = raw.hex(" ")
            response.set_result(raw)

    subscribed = False
    started = time.monotonic()
    cause = None
    cancelled = None
    try:
        if record["abort_requested"]:
            raise asyncio.CancelledError()
        record["cleanup_status"] = "SUBSCRIPTION_PENDING"
        record["subscribe_begin_monotonic_s"] = time.monotonic()
        await client.start_notify(indicate_uuid, on_indication)
        record["subscribe_return_monotonic_s"] = time.monotonic()
        subscribed = True
        record["cleanup_status"] = "SUBSCRIBED"
        if record["abort_requested"]:
            raise asyncio.CancelledError()
        # A notification while no request is outstanding is ambiguous.
        if seen:
            record["status"] = "UNSOLICITED_INDICATION"
            record["response_hex"] = seen[0].hex(" ")
            raise ConfigurationAckError(record)

        if marker is not None:
            try:
                marker_record = marker.mark()
                if (not isinstance(marker_record, dict)
                        or marker_record.get("status") != "ACK"):
                    raise RuntimeError(
                        "Timing marker did not return an ACK record")
                record["marker"] = marker_record
            except Exception as exc:
                record["status"] = "MARKER_ERROR"
                marker_record = getattr(exc, "record", None)
                if isinstance(marker_record, dict):
                    record["marker"] = marker_record
                record["marker_error"] = "{}: {}".format(
                    type(exc).__name__, exc)
                raise ConfigurationAckError(record) from exc

            # The marker handshake can take milliseconds. Any Matrix32
            # indication arriving during it is still unsolicited because the
            # configuration write has not yet been issued.
            if seen:
                record["status"] = "UNSOLICITED_INDICATION"
                record["response_hex"] = seen[0].hex(" ")
                raise ConfigurationAckError(record)

        pending[0] = True
        record["write_begin_monotonic_s"] = time.monotonic()
        await client.write_gatt_char(write_uuid, bytes(message), response=False)
        record["write_return_monotonic_s"] = time.monotonic()
        raw = await asyncio.wait_for(response, timeout=timeout_s)
        record["response_hex"] = raw.hex(" ")
        try:
            record["status"] = parse_config_ack(raw)
        except ValueError:
            record["status"] = "UNRECOGNIZED_RESPONSE"
            raise ConfigurationAckError(record)
        # No request ID; if two indications arrive in the same event loop turn,
        # never treat the first as a sufficient unambiguous result.
        await asyncio.sleep(0)
        if len(seen) != 1:
            record["status"] = "AMBIGUOUS_MULTIPLE_RESPONSES"
            raise ConfigurationAckError(record)
        if record["status"] != "OK":
            raise ConfigurationAckError(record)
    except asyncio.TimeoutError as exc:
        record["status"] = "TIMEOUT"
        cause = exc
    except ConfigurationAckError as exc:
        cause = exc
    except asyncio.CancelledError as exc:
        record["status"] = "CANCELLED"
        record["abort_requested"] = True
        cancelled = exc
    except Exception as exc:
        record["status"] = "TRANSPORT_ERROR"
        record["error"] = "{}: {}".format(type(exc).__name__, exc)
        cause = exc
    finally:
        pending[0] = False
        record["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        if subscribed:
            record["cleanup_status"] = "UNSUBSCRIBE_PENDING"
            record["unsubscribe_begin_monotonic_s"] = time.monotonic()
            try:
                await client.stop_notify(indicate_uuid)
                record["unsubscribe_return_monotonic_s"] = time.monotonic()
                record["cleanup_status"] = "UNSUBSCRIBED"
            except asyncio.CancelledError as exc:
                record["cleanup_status"] = "UNSUBSCRIBE_CANCELLED"
                record["status"] = "CANCELLED"
                cancelled = exc
            except Exception as exc:
                record["cleanup_status"] = "UNSUBSCRIBE_ERROR"
                record["status"] = "UNSUBSCRIBE_ERROR"
                record["error"] = "{}: {}".format(type(exc).__name__, exc)
                cause = exc
        elif record["cleanup_status"] == "SUBSCRIPTION_PENDING":
            # A cancelled/failed subscribe may have reached the peripheral.
            # Do not assert that indication state is clean in this case.
            record["cleanup_status"] = "SUBSCRIPTION_STATE_UNKNOWN"
        record["completed_monotonic_s"] = time.monotonic()
    if cancelled is not None:
        raise cancelled
    if cause is not None:
        raise ConfigurationAckError(record) from cause
    return record



class PersistentConfigAckSession:
    """Bounded config session with one persistent indication subscription.

    Exactly one command may be outstanding. The generic Matrix32 ACK has no
    request ID, so unsolicited, duplicate, malformed, or otherwise ambiguous
    indications fail the session closed.

    This helper underpins the qualified Matrix32Device live-frequency path and
    remains available to experiment runners that need the same transport
    semantics explicitly.
    """

    def __init__(self, client, write_uuid, indicate_uuid):
        self.client = client
        self.write_uuid = write_uuid
        self.indicate_uuid = indicate_uuid
        self.subscribe_begin_monotonic_s = None
        self.subscribe_return_monotonic_s = None
        self.unsubscribe_begin_monotonic_s = None
        self.unsubscribe_return_monotonic_s = None
        self.unsolicited_indications = []
        self._subscribed = False
        self._busy = False
        self._response = None
        self._record = None
        self._seen = None
        self._fatal_reason = None

    def _on_indication(self, _sender, data):
        raw = bytes(data)
        received = time.monotonic()
        item = {
            "monotonic_s": received,
            "response_hex": raw.hex(" "),
        }
        if self._record is None or self._response is None:
            self.unsolicited_indications.append(item)
            self._fatal_reason = "UNSOLICITED_INDICATION"
            return

        self._seen.append(raw)
        self._record["indications"].append(item)
        if not self._response.done():
            self._record["ack_monotonic_s"] = received
            self._record["response_hex"] = raw.hex(" ")
            self._response.set_result(raw)
        else:
            self._fatal_reason = "AMBIGUOUS_MULTIPLE_RESPONSES"

    async def open(self):
        if self._subscribed:
            raise RuntimeError("Persistent config ACK session already open")
        if not self.client.is_connected:
            raise RuntimeError("Cannot subscribe on disconnected BLE client")
        self.subscribe_begin_monotonic_s = time.monotonic()
        await self.client.start_notify(self.indicate_uuid, self._on_indication)
        self.subscribe_return_monotonic_s = time.monotonic()
        self._subscribed = True
        await asyncio.sleep(0)
        if self.unsolicited_indications:
            await self.close()
            raise RuntimeError(
                "Unsolicited Matrix32 indication observed while opening session")
        return self

    async def close(self):
        if not self._subscribed:
            return
        self.unsubscribe_begin_monotonic_s = time.monotonic()
        try:
            await self.client.stop_notify(self.indicate_uuid)
        finally:
            self.unsubscribe_return_monotonic_s = time.monotonic()
            self._subscribed = False
            self._record = None
            self._response = None
            self._seen = None
            self._busy = False

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
        return False

    async def send(
            self, message, timeout_s=DEFAULT_ACK_TIMEOUT_S, record=None,
            write_response=False):
        """Send one command and wait for its unique positive ACK.

        write_response=False uses an ATT Write Command (the established path).
        write_response=True uses an ATT Write Request/Response, but the standard
        ATT response is NOT treated as Matrix32 configuration verification; the
        unique application-level >OK< indication remains mandatory.

        The next command cannot begin until this method has completed. A second
        concurrent caller is rejected rather than queued.
        """
        if not (0 < timeout_s <= 10):
            raise ValueError(
                "Configuration acknowledgement timeout must be 0..10 s")
        if not self._subscribed:
            raise RuntimeError("Persistent config ACK session is not open")
        if not self.client.is_connected:
            raise RuntimeError("BLE disconnected during persistent ACK session")
        if self._busy:
            raise RuntimeError(
                "Persistent config ACK session permits one outstanding command")
        if self._fatal_reason or self.unsolicited_indications:
            raise RuntimeError(
                "Persistent config ACK session is ambiguous and cannot continue")

        command = config_command_name(message)
        if record is None:
            record = {}
        record.update({
            "command": command,
            "request_hex": bytes(message).hex(" "),
            "status": "NOT_VERIFIED",
            "response_hex": None,
            "request_monotonic_s": time.monotonic(),
            "write_begin_monotonic_s": None,
            "write_return_monotonic_s": None,
            "ack_monotonic_s": None,
            "completed_monotonic_s": None,
            "elapsed_ms": None,
            "indications": [],
            "write_response_requested": bool(write_response),
            "write_procedure": (
                "ATT_WRITE_REQUEST" if write_response
                else "ATT_WRITE_COMMAND"),
        })

        loop = asyncio.get_running_loop()
        self._response = loop.create_future()
        self._record = record
        self._seen = []
        self._busy = True
        started = time.monotonic()
        cause = None
        cancelled = None

        try:
            if record.get("abort_requested"):
                raise asyncio.CancelledError()
            record["write_begin_monotonic_s"] = time.monotonic()
            await self.client.write_gatt_char(
                self.write_uuid, bytes(message), response=bool(write_response))
            record["write_return_monotonic_s"] = time.monotonic()

            raw = await asyncio.wait_for(self._response, timeout=timeout_s)
            record["response_hex"] = raw.hex(" ")
            try:
                record["status"] = parse_config_ack(raw)
            except ValueError as exc:
                record["status"] = "UNRECOGNIZED_RESPONSE"
                cause = exc

            # Catch a duplicate indication delivered in the same event-loop turn.
            await asyncio.sleep(0)
            if len(self._seen) != 1 or self._fatal_reason:
                record["status"] = "AMBIGUOUS_MULTIPLE_RESPONSES"
                cause = ConfigurationAckError(record)
            elif record["status"] != "OK":
                cause = ConfigurationAckError(record)
        except asyncio.TimeoutError as exc:
            record["status"] = "TIMEOUT"
            cause = exc
        except asyncio.CancelledError as exc:
            record["status"] = "CANCELLED"
            cancelled = exc
        except Exception as exc:
            if record["status"] == "NOT_VERIFIED":
                record["status"] = "TRANSPORT_ERROR"
            record["error"] = "{}: {}".format(type(exc).__name__, exc)
            cause = exc
        finally:
            record["elapsed_ms"] = round(
                (time.monotonic() - started) * 1000, 3)
            record["completed_monotonic_s"] = time.monotonic()
            if cause is not None or cancelled is not None:
                self._fatal_reason = self._fatal_reason or record["status"]
            self._busy = False
            self._record = None
            self._response = None
            self._seen = None

        if cancelled is not None:
            raise cancelled
        if cause is not None:
            raise ConfigurationAckError(record) from cause
        return record
