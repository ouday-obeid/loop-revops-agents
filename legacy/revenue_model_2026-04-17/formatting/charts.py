"""Chart factory functions."""

from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.series import DataPoint
from openpyxl.utils import get_column_letter


def create_bar_chart(ws, title: str, data_ref: Reference,
                     cats_ref: Reference, width: int = 18, height: int = 10,
                     y_title: str = "", colors: list[str] | None = None,
                     style: int = 10) -> BarChart:
    """Create a bar chart and return it (caller adds to sheet)."""
    chart = BarChart()
    chart.type = "col"
    chart.style = style
    chart.title = title
    chart.y_axis.title = y_title
    chart.width = width
    chart.height = height
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    if colors:
        for i, color in enumerate(colors):
            if i < len(chart.series):
                chart.series[i].graphicalProperties.solidFill = color
    return chart


def create_combo_chart(ws, title: str, bar_ref: Reference, line_ref: Reference,
                       cats_ref: Reference, width: int = 18, height: int = 10,
                       bar_color: str = "2E75B6", line_color: str = "E74C3C") -> BarChart:
    """Bar + Line combo chart (target bars, actual line)."""
    chart = BarChart()
    chart.type = "col"
    chart.style = 10
    chart.title = title
    chart.width = width
    chart.height = height
    chart.add_data(bar_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    if len(chart.series) > 0:
        chart.series[0].graphicalProperties.solidFill = bar_color

    line = LineChart()
    line.add_data(line_ref, titles_from_data=True)
    if len(line.series) > 0:
        line.series[0].graphicalProperties.line.solidFill = line_color
    chart.y_axis.crosses = "min"
    chart += line
    return chart


def create_pie_chart(ws, title: str, data_ref: Reference,
                     cats_ref: Reference, width: int = 12, height: int = 10,
                     colors: list[str] | None = None) -> PieChart:
    """Create a pie chart."""
    chart = PieChart()
    chart.title = title
    chart.width = width
    chart.height = height
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    chart.dataLabels = DataLabelList()
    chart.dataLabels.showPercent = True
    chart.dataLabels.showVal = True
    if colors and len(chart.series) > 0:
        for i, color in enumerate(colors):
            pt = DataPoint(idx=i)
            pt.graphicalProperties.solidFill = color
            chart.series[0].data_points.append(pt)
    return chart


def create_line_chart(ws, title: str, data_ref: Reference,
                      cats_ref: Reference, width: int = 18, height: int = 10,
                      y_title: str = "", colors: list[str] | None = None) -> LineChart:
    """Create a line chart."""
    chart = LineChart()
    chart.style = 10
    chart.title = title
    chart.y_axis.title = y_title
    chart.width = width
    chart.height = height
    chart.add_data(data_ref, titles_from_data=True)
    chart.set_categories(cats_ref)
    if colors:
        for i, color in enumerate(colors):
            if i < len(chart.series):
                chart.series[i].graphicalProperties.line.solidFill = color
    return chart
