"""Per-connection XKB detectable autorepeat, absent from python-xlib's extensions.

Wire layout: https://xorg.freedesktop.org/archive/X11R7.7/doc/kbproto/xkbproto.html
Only this client's events change; system repeat and other applications are untouched.
"""

from Xlib.protocol import rq


class UseExtension(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8("opcode"), rq.Opcode(0), rq.RequestLength(), rq.Card16("major"), rq.Card16("minor")
    )
    _reply = rq.Struct(
        rq.Pad(1),
        rq.Bool("supported"),
        rq.Card16("sequence_number"),
        rq.Pad(4),
        rq.Card16("major"),
        rq.Card16("minor"),
        rq.Pad(20),
    )


class PerClientFlags(rq.ReplyRequest):
    _request = rq.Struct(
        rq.Card8("opcode"),
        rq.Opcode(21),
        rq.RequestLength(),
        rq.Card16("device"),
        rq.Pad(2),
        rq.Card32("change"),
        rq.Card32("value"),
        rq.Card32("controls_change"),
        rq.Card32("auto_controls"),
        rq.Card32("auto_values"),
    )
    _reply = rq.Struct(
        rq.Pad(1),
        rq.Card8("device"),
        rq.Card16("sequence_number"),
        rq.Pad(4),
        rq.Card32("supported"),
        rq.Card32("value"),
        rq.Card32("auto_controls"),
        rq.Card32("auto_values"),
        rq.Pad(8),
    )


def enable_detectable_repeat(connection) -> bool:
    extension = connection.query_extension("XKEYBOARD")
    if not extension.present:
        return False
    protocol = dict(display=connection.display, opcode=extension.major_opcode)
    if not UseExtension(**protocol, major=1, minor=0).supported:
        return False
    result = PerClientFlags(
        **protocol, device=0x0100, change=1, value=1, controls_change=0, auto_controls=0, auto_values=0
    )
    return bool(result.supported & result.value & 1)
