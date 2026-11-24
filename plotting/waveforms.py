"""Display original signal arrays without smoothing or downsampling.

Generic plot_traces input: 'time_us', 'values', 'label', 'color'.
Voltage wrapper plot_waveforms keeps the existing 'voltage_v' input schema.
The axes are caller supplied; traces are plotted in caller-provided order.
"""
from plotting.style import style_signal_axes


def plot_traces(ax, traces, *, xlim, xlabel, ylabel, title,
                linewidth=1.0, legend_fontsize=8, show_legend=True,
                legend_loc=None):
    """Draw original samples of any physical quantity; return line objects.

    Each trace: time_us, values, color, label. No filtering or resampling.
    """
    lines = []
    for trace in traces:
        line, = ax.plot(
            trace["time_us"], trace["values"], linewidth=linewidth,
            color=trace["color"], label=trace["label"],
        )
        lines.append(line)
    style_signal_axes(ax, xlim=xlim, xlabel=xlabel, ylabel=ylabel,
                      title=title)
    if show_legend:
        kwargs = {}
        if legend_fontsize is not None:
            kwargs["fontsize"] = legend_fontsize
        if legend_loc is not None:
            kwargs["loc"] = legend_loc
        ax.legend(**kwargs)
    return lines


def plot_waveforms(ax, traces, *, xlim, xlabel, ylabel, title,
                   linewidth=1.0, legend_fontsize=8, show_legend=True):
    """Backward-compatible voltage-specific wrapper with identical behavior."""
    return plot_traces(
        ax,
        ({"time_us": trace["time_us"], "values": trace["voltage_v"],
          "color": trace["color"], "label": trace["label"]}
         for trace in traces),
        xlim=xlim, xlabel=xlabel, ylabel=ylabel, title=title,
        linewidth=linewidth, legend_fontsize=legend_fontsize,
        show_legend=show_legend,
    )
