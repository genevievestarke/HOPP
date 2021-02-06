from typing import NamedTuple, Optional, Union
import numpy as np
from shapely.geometry import Point, Polygon
import PySAM.Pvwattsv7 as pv_simple
import PySAM.Pvsamv1 as pv_detailed

from hybrid.log import hybrid_logger as logger
from hybrid.sites import SiteInfo
from hybrid.layout.pv_module import module_width, module_height, modules_per_string, module_power
from hybrid.layout.plot_tools import plot_shape
from hybrid.layout.layout_tools import make_polygon_from_bounds
from hybrid.layout.solar_layout_tools import find_best_solar_size


class SolarGridParameters(NamedTuple):
    """
    x_position: ratio of solar's x coords to site width (0, 1)
    y_position: ratio of solar's y coords to site height (0, 1)
    aspect_power: aspect ratio of solar to site width = 2^solar_aspect_power
    gcr: gcr ratio of solar patch
    s_buffer: south side buffer ratio (0, 1)
    x_buffer: east and west side buffer ratio (0, 1)
    """
    x_position: float
    y_position: float
    aspect_power: float
    gcr: float
    s_buffer: float
    x_buffer: float


class SolarLayout:
    """

    """

    def __init__(self,
                 site_info: SiteInfo,
                 solar_source: Union[pv_simple.Pvwattsv7, pv_detailed.Pvsamv1],
                 parameters: Optional[SolarGridParameters] = None,
                 min_spacing: float = 100.
                 ):
        self.site: SiteInfo = site_info
        self._system_model: Union[pv_simple.Pvwattsv7, pv_detailed.Pvsamv1] = solar_source
        self.min_spacing = min_spacing

        self.module_power: float = module_power
        self.module_width: float = module_width
        self.module_height: float = module_height
        self.modules_per_string: int = modules_per_string

        # solar array layout variables
        self.parameters = parameters

        # layout design parameters
        self.num_modules: int = 0
        self.strands: list = []
        self.solar_region: Polygon = Polygon()
        self.buffer_region: Polygon = Polygon()

    def _get_system_config(self):
        if not isinstance(self._system_model, pv_detailed.Pvsamv1):
            return
        if self._system_model.Module.module_model == 1:
            self.module_width = self._system_model.CECPerformanceModelWithModuleDatabase.cec_module_width
            self.module_height = self._system_model.CECPerformanceModelWithModuleDatabase.cec_module_length
            self.module_power = self._system_model.CECPerformanceModelWithModuleDatabase.cec_v_mp_ref * \
                                self._system_model.CECPerformanceModelWithModuleDatabase.cec_i_mp_ref / 1000
            self.modules_per_string = self._system_model.SystemDesign.subarray1_modules_per_string
        else:
            raise NotImplementedError("Only CEC Module Model with Databse is allowed currently")

        if self._system_model.SystemDesign.subarray2_enable or self._system_model.SystemDesign.subarray3_enable \
            or self._system_model.SystemDesign.subarray4_enable:
            raise NotImplementedError("Only one subarray can be used in layout design")

    def _set_system_layout(self):
        system_capacity = self.module_power * self.num_modules

        if isinstance(self._system_model, pv_simple.Pvwattsv7):
            self._system_model.SystemDesign.gcr = self.parameters.gcr
            self._system_model.SystemDesign.system_capacity = system_capacity
        else:
            raise NotImplementedError("Modification of Detailed PV Layout not yet enabled")

        logger.info("Solra Layout set for {} kw system capacity".format(system_capacity))

    def reset_solargrid(self,
                        solar_capacity_kw: float,
                        parameters: SolarGridParameters = None):
        self.parameters = parameters
        self._get_system_config()

        max_num_modules = int(np.floor(solar_capacity_kw / self.module_power))

        site_sw_bound = np.array([self.site.polygon.bounds[0], self.site.polygon.bounds[1]])
        site_ne_bound = np.array([self.site.polygon.bounds[2], self.site.polygon.bounds[3]])
        site_bounds_size = site_ne_bound - site_sw_bound

        solar_center = site_sw_bound + site_bounds_size * \
                       np.array([parameters.x_position, parameters.y_position])

        # place solar
        max_solar_width = self.module_width * max_num_modules \
                          / self.modules_per_string

        solar_aspect = np.exp(parameters.aspect_power)
        solar_x_size, self.num_modules, self.strands, self.solar_region, solar_bounds = \
            find_best_solar_size(
                max_num_modules,
                self.modules_per_string,
                self.site.polygon,
                solar_center,
                0.0,
                self.module_width,
                self.module_height,
                parameters.gcr,
                solar_aspect,
                self.module_width,
                max_solar_width,
            )

        solar_x_buffer_length = self.min_spacing * (1 + parameters.x_buffer)
        solar_s_buffer_length = self.min_spacing * (1 + parameters.s_buffer)
        self.buffer_region = make_polygon_from_bounds(
            solar_bounds[0] - np.array([solar_x_buffer_length, solar_s_buffer_length]),
            solar_bounds[1] + np.array([solar_x_buffer_length, 0]))

        def get_bounds_center(shape):
            bounds = shape.bounds
            return Point(.5 * (bounds[0] + bounds[2]), .5 * (bounds[1] + bounds[3]))

        def get_excess_buffer_penalty(buffer, solar_region, bounding_shape):
            penalty = 0.0
            buffer_intersection = buffer.intersection(bounding_shape)

            shape_center = get_bounds_center(buffer)
            intersection_center = get_bounds_center(buffer_intersection)

            shape_center_delta = \
                np.abs(np.array(shape_center.coords) - np.array(intersection_center.coords)) / site_bounds_size
            shape_center_penalty = np.sum(shape_center_delta ** 2)
            penalty += shape_center_penalty

            bounds = buffer.bounds
            intersection_bounds = buffer_intersection.bounds

            west_excess = intersection_bounds[0] - bounds[0]
            south_excess = intersection_bounds[1] - bounds[1]
            east_excess = bounds[2] - intersection_bounds[2]
            north_excess = bounds[3] - intersection_bounds[3]

            solar_bounds = solar_region.bounds
            actual_aspect = (solar_bounds[3] - solar_bounds[1]) / \
                            (solar_bounds[2] - solar_bounds[0])

            aspect_error = np.abs(np.log(actual_aspect) - np.log(solar_aspect))
            penalty += aspect_error ** 2

            # excess buffer, minus minimum size
            # excess buffer is how much extra there is, but we must not penalise minimum sizes
            #
            # excess_x_buffer = max(0.0, es - min_spacing)
            # excess_y_buffer = max(0.0, min(ee, ew) - min_spacing)

            # if buffer has excess, then we need to penalize any excess buffer length beyond the minimum

            minimum_s_buffer = max(solar_s_buffer_length - south_excess, self.min_spacing)
            excess_x_buffer = (solar_s_buffer_length - minimum_s_buffer) / self.min_spacing
            penalty += excess_x_buffer ** 2

            minimum_w_buffer = max(solar_x_buffer_length - west_excess, self.min_spacing)
            minimum_e_buffer = max(solar_x_buffer_length - east_excess, self.min_spacing)
            excess_y_buffer = (solar_x_buffer_length - max(minimum_w_buffer, minimum_e_buffer)) / self.min_spacing
            penalty += excess_y_buffer ** 2

            return penalty

        penalty = get_excess_buffer_penalty(self.buffer_region, self.solar_region, self.site.polygon)

        self._set_system_layout()

        return penalty

    def set_layout_params(self,
                          params: SolarGridParameters):
        system_capacity = self.module_power * self.num_modules
        self.reset_solargrid(system_capacity, params)

    def set_system_capacity(self,
                            size_kw):
        """
        Changes system capacity in the existing layout
        """
        if self.parameters:
            self.reset_solargrid(size_kw, self.parameters)

    def plot(self,
             figure=None,
             axes=None,
             solar_color='darkorange',
             site_border_color='k',
             site_alpha=0.95,
             linewidth=4.0
             ):
        if not figure and not axes:
            figure, axes = self.site.plot(figure, axes, site_border_color, site_alpha, linewidth)

        plot_shape(figure, axes, self.solar_region, '-', color=solar_color)
        plot_shape(figure, axes, self.buffer_region, '--', color=solar_color)
