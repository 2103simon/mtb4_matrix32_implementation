"""Shared display styling; caller owns what each color represents."""

WIDTH_PALETTE = ("#1f77b4", "#2ca02c", "#9467bd", "#e377c2")
WAVEFORM_SIZE = (11, 5.2)
FIGURE_DPI = 160


def condition_colors(conditions, palette=WIDTH_PALETTE):
    """Stable condition-specific colors; caller defines meaning of conditions."""
    return {
        value: palette[i % len(palette)]
        for i, value in enumerate(sorted(set(conditions)))
    }


def width_colors(widths):
    """Backward-compatible S01 width palette with stable condition order."""
    return condition_colors(widths)


def style_signal_axes(ax, *, xlim, xlabel, ylabel, title):
    """Standard axes for original-sample voltage, current or other signals."""
    ax.set_xlim(*xlim)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(True, alpha=.25)


def style_voltage_axes(ax, *, xlim, xlabel, ylabel, title):
    """Backward-compatible voltage helper with unchanged visual behavior."""
    return style_signal_axes(ax, xlim=xlim, xlabel=xlabel,
                             ylabel=ylabel, title=title)
