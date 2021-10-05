#
#    Copyright (c) 2009-2021 Tom Keffer <tkeffer@gmail.com>
#    Copyright (c) 2017-2021 Kevin Locke <kevin@kevinlocke.name>
#
#    See the file LICENSE.txt for your full rights.
#
"""Generate JSON data files for plotly.js plots for up to an effective date.
Based on weewx.imagegenerator.ImageGenerator."""

from __future__ import absolute_import
from __future__ import with_statement

import cmath
import datetime
import itertools
import json
import locale
import logging
import math
import os.path
import time

import weeplot.genplot
import weeplot.utilities
import weeutil.logger
import weeutil.weeutil
import weewx.reportengine
import weewx.units
import weewx.xtypes
from weeutil.config import search_up, accumulateLeaves
from weeutil.weeutil import to_bool, to_int, to_float, TimeSpan
from weewx.units import ValueTuple

log = logging.getLogger(__name__)


# =============================================================================
#                    Class PlotlyJSONGenerator
# =============================================================================

class PlotlyJSONGenerator(weewx.reportengine.ReportGenerator):
    """Class for managing the plotly.js JSON generator."""

    # Map weeWX marker type to most similar plotly marker type
    _plotly_marker_types = {
        'box': 'square-open',
        'circle': 'circle-open',
        'cross': 'cross',
        'x': 'x',
        }

    def run(self):
        self.setup()
        self.gen_images(self.gen_ts)

    def setup(self):
        try:
            g = self.skin_dict['Labels']['Generic']
        except KeyError:
            g = {}
        try:
            t = self.skin_dict['Texts']
        except KeyError:
            t = {}
        # generic_dict will contain "generic" labels, such as "Outside Temperature"
        self.generic_dict = weeutil.weeutil.KeyDict(g)
        # text_dict contains translated text strings
        self.text_dict = weeutil.weeutil.KeyDict(t)
        self.image_dict = self.skin_dict['ImageGenerator']
        self.formatter  = weewx.units.Formatter.fromSkinDict(self.skin_dict)
        self.converter  = weewx.units.Converter.fromSkinDict(self.skin_dict)
        # ensure that the skin_dir is in the image_dict
        self.image_dict['skin_dir'] = os.path.join(
            self.config_dict['WEEWX_ROOT'],
            self.skin_dict['SKIN_ROOT'],
            self.skin_dict['skin'])
        # ensure that we are in a consistent right location
        os.chdir(self.image_dict['skin_dir'])

    def gen_images(self, gen_ts):
        """Generate plotly JSON files.

        The time scales will be chosen to include the given timestamp, with nice beginning and
        ending times.

        Args:
            gen_ts (int): The time around which plots are to be generated. This will also be used
                as the bottom label in the plots. [optional. Default is to use the time of the last
                record in the database.]
        """
        t1 = time.time()
        ngen = 0

        # determine how much logging is desired
        log_success = to_bool(search_up(self.image_dict, 'log_success', True))

        # Loop over each time span class (day, week, month, etc.):
        for timespan in self.image_dict.sections:

            # Now, loop over all plot names in this time span class:
            for plotname in self.image_dict[timespan].sections:

                # Accumulate all options from parent nodes:
                plot_options = accumulateLeaves(self.image_dict[timespan][plotname])

                plotgen_ts = gen_ts
                if not plotgen_ts:
                    binding = plot_options['data_binding']
                    db_manager = self.db_binder.get_manager(binding)
                    plotgen_ts = db_manager.lastGoodStamp()
                    if not plotgen_ts:
                        plotgen_ts = time.time()

                image_root = os.path.join(self.config_dict['WEEWX_ROOT'],
                                          plot_options['HTML_ROOT'])
                # Get the path that the image is going to be saved to:
                img_file = os.path.join(image_root, '%s.plotly.json' % plotname)

                # Check whether this plot needs to be done at all:
                if _skip_this_plot(plotgen_ts, plot_options, img_file):
                    continue

                # Generate the plot.
                plotly_data = self.gen_plot(plotgen_ts,
                                     plot_options,
                                     self.image_dict[timespan][plotname])

                # 'plot' will be None if skip_if_empty was truthy, and the plot contains no data
                if plotly_data:
                    # Create the subdirectory that the image is to be put in. Wrap in a try block
                    # in case it already exists.
                    try:
                        os.makedirs(os.path.dirname(img_file))
                    except OSError:
                        pass

                    try:
                        with open(img_file, 'w') as json_file:
                            json.dump(
                                plotly_data,
                                json_file,
                                # Use separators without spaces to reduce file size
                                separators=(',', ':'))
                        ngen += 1
                    except IOError as e:
                        log.error("Unable to save to file '%s' %s:", img_file, e)

        t2 = time.time()

        if log_success:
            log.info("Generated %d images for report %s in %.2f seconds",
                     ngen,
                     self.skin_dict['REPORT_NAME'], t2 - t1)

    def gen_plot(self, plotgen_ts, plot_options, plot_dict):
        """Generate a single plot image.

        Args:
            plotgen_ts: A timestamp for which the plot will be valid. This is generally the last
            datum to be plotted.

            plot_options: A dictionary of plot options.

            plot_dict: A section in a ConfigObj. Each subsection will contain data about plots
            to be generated

        Returns:
            An instance of weeplot.genplot.TimePlot or None. If the former, it will be ready
            to render. If None, then skip_if_empty was truthy and no valid data were found.
        """

        # Create a new instance of a time plot and start adding to it
        plot = weeplot.genplot.TimePlot(plot_options)

        # Calculate a suitable min, max time for the requested time.
        minstamp, maxstamp, timeinc = weeplot.utilities.scaletime(
            plotgen_ts - int(plot_options.get('time_length', 86400)), plotgen_ts)
        x_domain = weeutil.weeutil.TimeSpan(minstamp, maxstamp)

        # Override the x interval if the user has given an explicit interval:
        timeinc_user = to_int(plot_options.get('x_interval'))
        if timeinc_user is not None:
            timeinc = timeinc_user

        # Set the y-scaling, using any user-supplied hints:
        yscale = plot_options.get('yscale', ['None', 'None', 'None'])
        yscale = weeutil.weeutil.convertToFloat(yscale)

        # Get a suitable bottom label:
        bottom_label_format = plot_options.get('bottom_label_format', '%m/%d/%y %H:%M')
        bottom_label = time.strftime(bottom_label_format, time.localtime(plotgen_ts))

        # Calculate the domain over which we should check for non-null data. It will be
        # 'None' if we are not to do the check at all.
        check_domain = _get_check_domain(plot_options.get('skip_if_empty', False), x_domain)

        # Set to True if we have _any_ data for the plot
        have_data = False

        padding = plot.padding / plot.anti_alias
        # plotly padding overlaps margins.
        # Add padding for consistency with ImageGenerator.
        lmargin = plot.lmargin / plot.anti_alias + padding
        rmargin = plot.rmargin / plot.anti_alias + padding
        bmargin = plot.bmargin / plot.anti_alias + padding
        tmargin = plot.tmargin / plot.anti_alias + padding
        margin = {
            'l': lmargin,
            'r': rmargin,
            'b': bmargin,
            't': tmargin,
            'pad': padding,
            }

        plot_w = plot.image_width // plot.anti_alias - lmargin - rmargin
        plot_h = plot.image_height // plot.anti_alias - lmargin - rmargin
        plotsize = plot_w, plot_h

        x_label_format = plot.x_label_format
        if x_label_format is None:
            x_label_format = _get_time_format(minstamp, maxstamp)

        # Initialize variables used in and after loop
        last_vector_options = None
        last_vector_line_num = 0
        unit_label = None

        # Loop over each line to be added to the plot.
        data = []
        line_num = -1;
        for line_name in plot_dict.sections:
            line_num += 1

            # Accumulate options from parent nodes.
            line_options = accumulateLeaves(plot_dict[line_name])
            # accumulateLeaves does not preserve .name
            line_options.name = line_name

            # See what observation type to use for this line. By default, use the section
            # name.
            var_type = line_options.get('data_type', line_name)

            # Find the database
            binding = line_options['data_binding']
            db_manager = self.db_binder.get_manager(binding)

            # If we were asked, see if there is any non-null data in the plot
            skip = _skip_if_empty(db_manager, var_type, check_domain)
            if skip:
                # Nothing but null data. Skip this line and keep going
                continue
            # Either we found some non-null data, or skip_if_empty was false, and we don't care.
            have_data = True

            # Look for aggregation type:
            aggregate_type = line_options.get('aggregate_type')
            if aggregate_type in (None, '', 'None', 'none'):
                # No aggregation specified.
                aggregate_type = aggregate_interval = None
            else:
                try:
                    # Aggregation specified. Get the interval.
                    aggregate_interval = weeutil.weeutil.nominal_spans(
                        line_options['aggregate_interval'])
                except KeyError:
                    log.error("Aggregate interval required for aggregate type %s",
                              aggregate_type)
                    log.error("Line type %s skipped", var_type)
                    continue

            # we need to pass the line options and plotgen_ts to our xtype
            # first get a copy of line_options
            option_dict = dict(line_options)
            # but we need to pop off aggregate_type and
            # aggregate_interval as they are used as explicit arguments
            # in our xtypes call
            option_dict.pop('aggregate_type', None)
            option_dict.pop('aggregate_interval', None)
            # then add plotgen_ts
            option_dict['plotgen_ts'] = plotgen_ts
            try:
                start_vec_t, stop_vec_t, data_vec_t = weewx.xtypes.get_series(
                    var_type,
                    x_domain,
                    db_manager,
                    aggregate_type=aggregate_type,
                    aggregate_interval=aggregate_interval,
                    **option_dict)
            except weewx.UnknownType:
                # If skip_if_empty is set, it's OK if a type is unknown.
                if not skip:
                    raise

            # Get the type of plot ('bar', 'line', or 'vector')
            plot_type = line_options.get('plot_type', 'line').lower()

            if aggregate_type and plot_type != 'bar':
                # If aggregating, put the point in the middle of the interval
                start_vec_t = ValueTuple(
                    [x - aggregate_interval / 2.0 for x in start_vec_t[0]],  # Value
                    start_vec_t[1],  # Unit
                    start_vec_t[2])  # Unit group
                stop_vec_t = ValueTuple(
                    [x - aggregate_interval / 2.0 for x in stop_vec_t[0]],  # Velue
                    stop_vec_t[1],  # Unit
                    stop_vec_t[2])  # Unit group

            # Convert the data to the requested units
            new_data_vec_t = self.converter.convert(data_vec_t)

            # Add a unit label. NB: all will get overwritten except the last. Get the label
            # from the configuration dictionary.
            unit_label = line_options.get(
                'y_label', self.formatter.get_label_string(new_data_vec_t[1]))
            # Strip off any leading and trailing whitespace so it's easy to center
            unit_label = unit_label.strip()

            # Remove missing data, for the following reasons:
            # - Avoids checks during conversion/scaling.
            # - Reduces JSON file size.
            # - Does not cause line breaks in line plots.
            new_data_values = new_data_vec_t[0]
            if None in new_data_values:
                start_vec_t = ValueTuple(
                    [v for i, v in enumerate(start_vec_t[0])
                     if new_data_values[i] is not None],
                    start_vec_t[1],
                    start_vec_t[2]
                    )
                stop_vec_t = ValueTuple(
                    [v for i, v in enumerate(stop_vec_t[0])
                     if new_data_values[i] is not None],
                    stop_vec_t[1],
                    stop_vec_t[2]
                    )
                new_data_vec_t = ValueTuple(
                    [v for v in new_data_values if v is not None],
                    new_data_vec_t[1],
                    new_data_vec_t[2]
                    )

            # See if a line label has been explicitly requested:
            label = line_options.get('label')
            if label:
                # Yes. Get the text translation
                label = self.text_dict[label]
            else:
                # No explicit label. Look up a generic one.
                # NB: generic_dict is a KeyDict which will substitute the key
                # if the value is not in the dictionary.
                label = self.generic_dict[var_type]

            # See if a color has been explicitly requested.
            color = line_options.get('color')
            if color is not None:
                color = weeplot.utilities.tobgr(color)
            else:
                color = plot.chart_line_colors[
                    line_num % len(plot.chart_line_colors)]

            fill_color = line_options.get('fill_color')
            if fill_color is not None:
                fill_color = weeplot.utilities.tobgr(fill_color)
            else:
                fill_color = plot.chart_fill_colors[
                    line_num % len(plot.chart_fill_colors)]

            # Get the line width, if explicitly requested.
            width = to_int(line_options.get('width'))
            if width is None:
                width = plot.chart_line_widths[
                    line_num % len(plot.chart_line_widths)]

            interval_vec = None
            gap_fraction = None
            vector_rotate = None

            # Some plot types require special treatments:
            if plot_type == 'vector':
                vector_rotate_str = line_options.get('vector_rotate')
                vector_rotate = -float(vector_rotate_str) \
                    if vector_rotate_str is not None else None
            elif plot_type == 'bar':
                interval_vec = [x[1] - x[0] for x in
                                zip(start_vec_t.value, stop_vec_t.value)]
            elif plot_type == 'line':
                gap_fraction = to_float(line_options.get('line_gap_fraction'))
                if gap_fraction is not None and not 0 < gap_fraction < 1:
                    log.error("Gap fraction %5.3f outside range 0 to 1. Ignored.",
                              gap_fraction)
                    gap_fraction = None
            else:
                log.error("Unknown plot type '%s'. Ignored", plot_type)
                continue

            # Get the type of line (only 'solid' or 'none' for now)
            line_type = line_options.get('line_type', 'solid')
            if line_type.strip().lower() in ['', 'none']:
                line_type = None

            marker_type = line_options.get('marker_type')
            marker_size = to_int(line_options.get('marker_size', 8))

            # Add the line to the emerging plot:
            data.extend(self._gen_line(
                stop_vec_t[0], new_data_vec_t[0],
                label=label,
                color=color,
                fill_color=fill_color,
                width=width,
                plot_type=plot_type,
                line_type=line_type,
                marker_type=marker_type,
                marker_size=marker_size,
                bar_width=interval_vec,
                vector_rotate=vector_rotate,
                gap_fraction=gap_fraction,
                x_label_format=x_label_format,
                y_label=unit_label,
                minstamp=minstamp,
                maxstamp=maxstamp,
                yscale=yscale,
                plotsize=plotsize))

            if line_options.get('plot_type') == 'vector':
                last_vector_line_num = line_num
                last_vector_options = line_options

        plotly_data = self._gen_plotly(
            plot                 = plot,
            data                 = data,
            plot_options         = plot_options,
            plotsize             = plotsize,
            margin               = margin,
            minstamp             = minstamp,
            maxstamp             = maxstamp,
            yscale               = yscale,
            x_label_format       = x_label_format,
            y_label              = unit_label,
            bottom_label         = bottom_label,
            last_vector_options  = last_vector_options,
            last_vector_line_num = last_vector_line_num,
            )

        # Return the constructed plot if it has any non-null data, otherwise return None
        return plotly_data if have_data else None


    def _gen_line(
            self,
            x,
            y,
            # Keyword-only arguments.  Enable for python3.
            # *,
            label,
            color,
            fill_color,
            width,
            plot_type,
            line_type,
            marker_type,
            marker_size,
            bar_width,
            vector_rotate,
            gap_fraction,
            x_label_format,
            y_label,
            minstamp,
            maxstamp,
            yscale,
            plotsize,
            ):
        if marker_type is not None:
            marker_type = marker_type.lower()
            if marker_type == 'none':
                marker_type = None

        if line_type is not None:
            if marker_type is not None:
                line_mode = 'lines+markers'
            else:
                line_mode = 'lines'
        elif marker_type is not None:
            line_mode = 'markers'
        else:
            line_mode = 'none'

        if gap_fraction is not None and plot_type != 'vector':
            maxdx = (maxstamp - minstamp) * gap_fraction
            x, y = _add_gaps(x, y, maxdx)

        line_data = {
            'name': label,
            'x': [_time_to_iso(t) for t in x],
            'y': y,
        }
        if plot_type == 'line':
            if marker_type:
                symbol = PlotlyJSONGenerator._plotly_marker_types[marker_type]
            else:
                symbol = None
            line_data.update({
                'type': 'scatter',
                'mode': line_mode,
                'connectgaps': False,
                'fillcolor': _bgr_to_css(fill_color),
                'line': {
                    'color': _bgr_to_css(color),
                    'dash': line_type,
                    'width': width,
                    },
                'marker': {
                    'symbol': symbol,
                    'size': marker_size,
                    },
                })
            return (line_data,)
        elif plot_type == 'bar':
            if bar_width and _all_equal(bar_width):
                # Uniform widths can be a single value to reduce space
                bar_width_ms = bar_width[0] * 1000
            else:
                # x1000 since plotly.js works in ms not seconds
                bar_width_ms = [i * 1000 for i in bar_width]
            line_data.update({
                'type': 'bar',
                'width': bar_width_ms,
                'marker': {
                    'color': _bgr_to_css(fill_color),
                    'line': {
                        'color': _bgr_to_css(color),
                        'width': width,
                        },
                    },
                })
            return (line_data,)
        elif plot_type == 'vector':
            line_data.update({
                'type': 'scatter',
                # Note: genplot doesn't draw markers for vector.
                'mode': 'lines',
                # Hide x/y hoverinfo since values are misleading
                'hoverinfo': 'text',
                })
            if line_type is not None:
                line_data['line'] = {
                    'color': _bgr_to_css(color),
                    'dash': line_type,
                    'width': width,
                    }
            # If there are no data points, return line data for use in legend
            if not y:
                return (line_data,)
            # Hide legend entries for lines by default un-hide last line only
            line_data['showlegend'] = False
            polar = [cmath.polar(d) for d in y]
            # Convert rotated complex y into un-rotated real y and z values
            if vector_rotate:
                vector_rotate_rad = math.radians(vector_rotate)
                vector_rotate_mul = complex(math.cos(vector_rotate_rad),
                                            math.sin(vector_rotate_rad))
                rotated = [d * vector_rotate_mul if d is not None else None
                           for d in y]
            else:
                rotated = y
            y = [d.imag for d in rotated]
            z = [d.real for d in rotated]

            xpscale = float(maxstamp - minstamp) / plotsize[0]
            if yscale[0] is not None and yscale[1] is not None:
                yrange = yscale[1] - yscale[0]
            else:
                yrange = max(y) - min(y)
            ypscale = float(yrange) / plotsize[1]
            xypscale = xpscale / ypscale

            lines = []
            for x0, y0, z0, (r, phi) in zip(x, y, z, polar):
                line = line_data.copy()
                line['x'] = [
                    _time_to_iso(x0),
                    _time_to_iso(x0 + z0 * xypscale)
                    ]
                line['y'] = [0, y0]
                x0_str = time.strftime(x_label_format, time.localtime(x0))
                # Inverse of complex conversion in getSqlVectors
                deg = (90 - math.degrees(phi)) % 360
                # Use 360 for non-zero North wind by convention
                deg = 360 if r > 0.001 and deg < 1 else deg
                line['text'] = locale.format_string(
                    u"%s: %.03f %s %d\u00B0",
                    (x0_str, r, y_label, deg))
                lines.append(line)
            # Show one line in legend
            del lines[-1]['showlegend']
            return lines
        else:
            raise AssertionError("Unrecognized plot type '%s'" % plot_type)


    def _gen_plotly(
            self,
            # Keyword-only arguments.  Enable for python3.
            # *,
            plot,
            data,
            plot_options,
            plotsize,
            margin,
            minstamp,
            maxstamp,
            yscale,
            x_label_format,
            y_label,
            bottom_label,
            last_vector_options,
            last_vector_line_num,
            ):

        top_label_font_family = plot_options.get('top_label_font_family')

        plot_w, plot_h = plotsize
        # Offset (in px) from plot area to top bar
        tb_off = margin['pad'] + float(plot.tmargin - plot.tbandht) / plot.anti_alias
        # Title y position in paper coordinates
        title_y = 1 + tb_off / plot_h

        shapes = []
        annotations = []

        layout = {
            'height': plot.image_height / plot.anti_alias,
            'width': plot.image_width / plot.anti_alias,
            'showlegend': False,
            'legend': {
                'bgcolor': _bgr_to_css(plot.chart_background_color),
                'font': {
                    'family': top_label_font_family,
                    'size': plot.top_label_font_size / plot.anti_alias,
                    },
                'orientation': 'h',
                'x': 0.5,
                'xanchor': 'center',
                'y': title_y,
                'yanchor': 'bottom',
                },
            'paper_bgcolor': _bgr_to_css(plot.image_background_color),
            'plot_bgcolor': _bgr_to_css(plot.chart_background_color),
            'titlefont': {
                'family': top_label_font_family,
                'size': plot.top_label_font_size / plot.anti_alias,
                },
            'xaxis': {
                # Note: type is required to set axis range when x data is empty
                # See https://github.com/plotly/plotly.js/issues/3487
                'type': 'date',
                'range': [_time_to_iso(minstamp), _time_to_iso(maxstamp)],
                # Note: Linear doesn't change on zoom, use auto.
                #'tickmode': 'linear',
                #'tick0': _time_to_iso(minstamp),
                #'dtick': timeinc * 1000,
                'tickmode': 'auto',
                'nticks': plot.x_nticks,
                'tickfont': {
                    'family': plot_options.get('axis_label_font_family'),
                    'size': plot.axis_label_font_size / plot.anti_alias,
                    'color': _bgr_to_css(plot.axis_label_font_color),
                    },
                'tickformat': x_label_format,
                'title': bottom_label,
                'titlefont': {
                    'family': plot_options.get('bottom_label_font_family'),
                    'size': plot.bottom_label_font_size / plot.anti_alias,
                    'color': _bgr_to_css(plot.bottom_label_font_color),
                    },
                'gridcolor': _bgr_to_css(plot.chart_gridline_color),
                },
            'yaxis': {
                'tickmode': 'auto',
                'nticks': plot.y_nticks,
                'tickformat': plot.y_label_format,
                'tickfont': {
                    'family': plot_options.get('axis_label_font_family'),
                    'size': plot.axis_label_font_size / plot.anti_alias,
                    'color': _bgr_to_css(plot.axis_label_font_color),
                    },
                'title': y_label,
                'titlefont': {
                    'family': plot_options.get('unit_label_font_family'),
                    'size': plot.unit_label_font_size / plot.anti_alias,
                    'color': _bgr_to_css(plot.unit_label_font_color),
                    },
                'gridcolor': _bgr_to_css(plot.chart_gridline_color),
                },
            'margin': margin,
            'shapes': shapes,
            'annotations': annotations,
            # Note: Default changed to closest in Plotly 2.0:
            # https://github.com/plotly/plotly.js/pull/5647
            'hovermode': 'x',
            }

        if yscale[0] is not None and yscale[1] is not None:
            layout['yaxis']['range'] = yscale[0:2]

        if plot.image_background_color != plot.chart_background_color:
            # Add top bar with chart bg color to match ImageGenerator
            shapes.append({
                'type': 'rect',
                'layer': 'below',
                'fillcolor': _bgr_to_css(plot.chart_background_color),
                'line': {
                    'width': 0
                },
                'xref': 'paper',
                'x0': -1,
                'x1': 2,
                'yref': 'paper',
                'y0': title_y,
                'y1': 2,
                })

        if plot.show_daynight:
            shapes += self._gen_daynight(plot, minstamp, maxstamp)

        # Draw compass rose if there is a vector line
        # Note: Must be after daynight to render above daynight
        if last_vector_options is not None:
            # Compass rose color and rotation affected by last vector line
            rose_rotate = to_float(last_vector_options.get('vector_rotate'))
            rose_color = plot.rose_color
            if rose_color is None:
                rose_color = last_vector_options.get('color')
                if rose_color is not None:
                    rose_color = weeplot.utilities.tobgr(rose_color)
                else:
                    rose_color = plot.chart_line_colors[
                        last_vector_line_num % len(plot.chart_line_colors)]
            # genplot hard-codes rose_position 5 from left plot edge
            # add one from bottom edge to separate 0/180 rose from edge
            rose_offset = 5, 1

            rose_shapes = _make_rose_shapes(
                plot.rose_height,
                plot.rose_diameter,
                # genplot hard-codes barb_width/barb_height of 3
                3,
                rose_color)
            if rose_rotate:
                # https://github.com/PyCQA/pylint/issues/1472
                # pylint: disable=invalid-unary-operand-type
                rose_shapes = _rotate_shapes(rose_shapes, -rose_rotate)
                # pylint: enable=invalid-unary-operand-type
            rose_shapes = _translate_shapes(
                rose_shapes,
                plot.rose_width / 2.0 + rose_offset[0],
                plot.rose_height / 2.0 + rose_offset[1])
            rose_shapes = _scale_shapes(rose_shapes, 1.0 / plot_w, 1.0 / plot_h)
            shapes += rose_shapes

            annotations.append({
                'text': plot.rose_label,
                # Size of text box in pixels.
                # Text is aligned in center/middle of box by default.
                'width': plot.rose_width,
                'height': plot.rose_height,
                'borderpad': 0,
                'borderwidth': 0,
                'font': {
                    'family': plot_options.get('rose_label_font_family'),
                    'size': plot.rose_label_font_size,
                    'color': _bgr_to_css(plot.rose_label_font_color),
                    },
                'showarrow': False,
                'xref': 'paper',
                'yref': 'paper',
                'x': float(rose_offset[0]) / plot_w,
                'y': float(rose_offset[1]) / plot_h,
                })

        # Add legend-as-title annotations
        titles = [
            {
                'text': line_data['name'],
                'font': {
                    'family': top_label_font_family,
                    'size': plot.top_label_font_size / plot.anti_alias,
                    'color':
                        line_data['line']['color'] if 'line' in line_data
                        else line_data['marker']['line']['color'],
                    },
                'showarrow': False,
                'xref': 'paper',
                'yref': 'paper',
                'x': 0.5,
                'y': title_y,
                'xanchor': 'center',
                'yanchor': 'bottom',
                'borderpad': 0,
                'borderwidth': 0,
                # Default SVG <text> height is larger than genplot sizing
                # Constrain height to get same y positioning of text
                'height': plot.tbandht / plot.anti_alias,
            }
            for line_data in data if line_data.get('showlegend', True)]
        # Calculate the x bounds of the plot in paper coordinates
        paper_xmin = float(-margin['l']) / plot_w
        paper_xmax = 1.0 + float(margin['r']) / plot_w
        paper_xrange = paper_xmax - paper_xmin
        # Space the center of the titles evenly across the paper xrange
        for i, title in enumerate(titles):
            title['x'] = paper_xmin + paper_xrange * (i + 1.0) / (len(titles) + 1.0)

        annotations += titles

        # Send list of fonts to facilitate pre-loading, with FontFace schema:
        # https://drafts.csswg.org/css-font-loading/#fontface
        font_families = set((
            top_label_font_family,
            plot_options.get('axis_label_font_family'),
            plot_options.get('bottom_label_font_family'),
            plot_options.get('unit_label_font_family'),
            ))
        if last_vector_options is not None:
            font_families.add(plot_options.get('rose_label_font_family'))
        font_families.remove(None)
        fonts = [{'family': family} for family in font_families]

        return {'data': data, 'fonts': fonts, 'layout': layout}


    def _gen_daynight(self, plot, minstamp, maxstamp):
        """Generate background shapes for day/night display"""
        daynight_day_color = _bgr_to_css(plot.daynight_day_color)
        daynight_night_color = _bgr_to_css(plot.daynight_night_color)
        daynight_edge_color = _bgr_to_css(plot.daynight_edge_color)

        first, transitions = weeutil.weeutil.getDayNightTransitions(
            minstamp,
            maxstamp,
            self.stn_info.latitude_f,
            self.stn_info.longitude_f)
        transitions.insert(0, minstamp)
        transitions.append(maxstamp)
        transitions = [_time_to_iso(t) for t in transitions]
        is_days = itertools.cycle((first == 'day', first != 'day'))
        # Note: daynight edge line must be separate shape due to
        # top/bottom of rect.  Hide rect line.
        # FIXME: Plotly.js doesn't support gradient for daynight_gradient
        daynight_line = {'width': 0}
        daynight_shapes = [
            {
                'type': 'rect',
                'layer': 'below',
                'fillcolor':
                    daynight_day_color if is_day else daynight_night_color,
                'line': daynight_line,
                'xref': 'x',
                'x0': x0,
                'x1': x1,
                'yref': 'paper',
                'y0': 0,
                'y1': 1,
            }
            for is_day, x0, x1
            in zip(is_days, transitions, transitions[1:])]

        if (daynight_edge_color != daynight_day_color and
                daynight_edge_color != daynight_night_color):
            # Note: riseset must be after daynight to draw above.
            riseset_line = {'color': daynight_edge_color}
            daynight_shapes += (
                {
                    'type': 'line',
                    'layer': 'below',
                    'line': riseset_line,
                    'xref': 'x',
                    'x0': x,
                    'x1': x,
                    'yref': 'paper',
                    'y0': 0,
                    'y1': 1,
                }
                for x in transitions[1:-1])

        return daynight_shapes


def _add_gaps(x, y, maxdx):
    """Creates lists of x and y values with additional None-valued points to
    create gaps for x steps larger than maxdx.

    More precisely: For consecutive points (x0, y0) and (x1, y1), adds
    (avg(x0, x1), None) if y0 and y1 are not None and x1 - x0 > maxdx."""
    gx = []
    gy = []
    x0 = None
    for x1, y1 in zip(x, y):
        if x0 is not None and y1 is not None:
            dx = x1 - x0
            if dx > maxdx:
                gx.append(x1 - dx / 2)
                gy.append(None)
        gx.append(x1)
        gy.append(y1)
        x0 = None if y1 is None else x1
    return gx, gy


def _bgr_to_css(bgr):
    """Gets a CSS color for a given little-endian integer color value."""
    blue = (bgr & 0xFF0000) >> 16
    green = (bgr & 0x00FF00) >> 8
    red = (bgr & 0x0000FF)
    return "#%02x%02x%02x" % (red, green, blue)


def _get_time_format(minstamp, maxstamp):
    """Gets a locale format string for time values in a given range."""
    # FIXME: Duplicated with TimePlot._calcXLabelFormat
    delta = maxstamp - minstamp
    if delta > 30 * 24 * 3600:
        return u"%x"
    if delta > 24 * 3600:
        return u"%x %X"
    return u"%X"


def _all_equal(iterable):
    """
    Check if all values in an :class:`Iterable` are equal.

    Note: Same signature and behavior as ``more_itertools.all_equal``,
    different implementation (which checks for equality to first value rather
    than using ``itertools.groupby``).

    Args:
        iterable (Iterable[Any]): Iterable of values to check for equality.

    Returns:
        bool: ``False`` if ``iterable`` contains two values which are not equal
            to each other, otherwise ``True``.
    """
    iterator = iter(iterable)
    try:
        first = next(iterator)
    except StopIteration:
        # Empty iterable.  Match more_itertools.all_equal behavior.
        return True
    for val in iterator:
        if val != first:
            return False
    return True


def _make_rose_shapes(length, diameter, barb_size, color):
    """Creates plotly shapes for a compass rose centered at the origin pointing
    along the positive y axis."""
    line = {
        'color': _bgr_to_css(color),
        'width': 1
        }
    half_diam = diameter / 2.0
    half_length = length / 2.0
    shaft = {
        'type': 'line',
        'xref': 'paper',
        'yref': 'paper',
        'x0': 0,
        'y0': -half_length,
        'x1': 0,
        'y1': half_length,
        'line': line,
        }
    barb1 = {
        'type': 'line',
        'xref': 'paper',
        'yref': 'paper',
        'x0': -barb_size,
        'y0': half_length - barb_size,
        'x1': 0,
        'y1': half_length,
        'line': line,
        }
    barb2 = {
        'type': 'line',
        'xref': 'paper',
        'yref': 'paper',
        'x0': barb_size,
        'y0': half_length - barb_size,
        'x1': 0,
        'y1': half_length,
        'line': line,
        }
    circle = {
        'type': 'circle',
        'xref': 'paper',
        'yref': 'paper',
        'x0': -half_diam,
        'y0': -half_diam,
        'x1': half_diam,
        'y1': half_diam,
        'line': line,
        }

    return shaft, barb1, barb2, circle


def _rotate_shapes(shapes, rotation):
    """Rotate shapes around the origin by a given angle in degrees."""
    if rotation:
        rotation = math.radians(rotation)
        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)
        for shape in shapes:
            for xname, yname in ('x0', 'y0'), ('x1', 'y1'):
                x = shape[xname]
                y = shape[yname]
                shape[xname] = x * cos_r - y * sin_r
                shape[yname] = x * sin_r + y * cos_r
    return shapes


def _scale_shapes(shapes, sx, sy):
    """Scale shapes by a given x and y scale factor."""
    if sx != 1 or sy != 1:
        for shape in shapes:
            for xname, yname in ('x0', 'y0'), ('x1', 'y1'):
                shape[xname] *= sx
                shape[yname] *= sy
    return shapes


def _translate_shapes(shapes, dx, dy):
    """Translate (move) shapes by a given x and y distance."""
    if dx or dy:
        for shape in shapes:
            for xname, yname in ('x0', 'y0'), ('x1', 'y1'):
                shape[xname] += dx
                shape[yname] += dy

    return shapes


def _time_to_iso(time_ts):
    """Converts a timestamp (seconds since epoch) to the RFC 3339 profile of
    the ISO 8601 format in the local timezone."""
    local_dt = datetime.datetime.fromtimestamp(time_ts)
    utc_dt = datetime.datetime.utcfromtimestamp(time_ts)
    offset_sec = (local_dt - utc_dt).total_seconds()
    (offset_hr, offset_min) = divmod(offset_sec // 60, 60)
    return local_dt.isoformat() + ('%+03d:%02d' % (offset_hr, offset_min))


def _skip_this_plot(time_ts, plot_options, img_file):
    """A plot can be skipped if it was generated recently and has not changed. This happens if the
    time since the plot was generated is less than the aggregation interval.

    If a stale_age has been specified, then it can also be skipped if the file has been
    freshly generated.
    """

    # Convert from possible string to an integer:
    aggregate_interval = weeutil.weeutil.nominal_spans(plot_options.get('aggregate_interval'))

    # Images without an aggregation interval have to be plotted every time. Also, the image
    # definitely has to be generated if it doesn't exist.
    if aggregate_interval is None or not os.path.exists(img_file):
        return False

    # If its a very old image, then it has to be regenerated
    if time_ts - os.stat(img_file).st_mtime >= aggregate_interval:
        return False

    # If we're on an aggregation boundary, regenerate.
    time_dt = datetime.datetime.fromtimestamp(time_ts)
    tdiff = time_dt -  time_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if abs(tdiff.seconds % aggregate_interval) < 1:
        return False

    # Check for stale plots, but only if 'stale_age' is defined
    stale = to_int(plot_options.get('stale_age'))
    if stale:
        t_now = time.time()
        try:
            last_mod = os.path.getmtime(img_file)
            if t_now - last_mod < stale:
                log.debug("Skip '%s': last_mod=%s age=%s stale=%s",
                          img_file, last_mod, t_now - last_mod, stale)
                return True
        except os.error:
            pass
    return False


def _get_check_domain(skip_if_empty, x_domain):
    # Convert to lower-case. It might not be a string, so be prepared for an AttributeError
    try:
        skip_if_empty = skip_if_empty.lower()
    except AttributeError:
        pass
    # If it's something we recognize as False, return None
    if skip_if_empty in ['false', False, None]:
        return None
    # If it's True, then return the existing time domain
    elif skip_if_empty in ['true', True]:
        return x_domain
    # Otherwise, it's probably a string (such as 'day', 'month', etc.). Return the corresponding
    # time domain
    else:
        return weeutil.weeutil.timespan_by_name(skip_if_empty, x_domain.stop)


def _skip_if_empty(db_manager, var_type, check_domain):
    """

    Args:
        db_manager: An open instance of weewx.manager.Manager, or a subclass.

        var_type: An observation type to check (e.g., 'outTemp')

        check_domain: A two-way tuple of timestamps that contain the time domain to be checked
        for non-null data.

    Returns:
        True if there is no non-null data in the domain. False otherwise.
    """
    if check_domain is None:
        return False
    try:
        val = weewx.xtypes.get_aggregate(var_type, check_domain, 'not_null', db_manager)
    except weewx.UnknownAggregation:
        return True
    return not val[0]
