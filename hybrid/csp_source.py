from typing import Optional, Union, Sequence

import rapidjson                # NOTE: install 'python-rapidjson' NOT 'rapidjson'

import pandas as pd
import numpy as np
import datetime
import os

from hybrid.pySSC_daotk.ssc_wrap import ssc_wrap
import PySAM.Singleowner as Singleowner

from hybrid.dispatch.power_sources.csp_dispatch import CspDispatch
from hybrid.power_source import PowerSource
from hybrid.sites import SiteInfo


class CspPlant(PowerSource):
    _system_model: None
    _financial_model: Singleowner
    # _layout: TroughLayout
    _dispatch: CspDispatch

    def __init__(self,
                 name: str,
                 tech_name: str,
                 site: SiteInfo,
                 financial_model: Singleowner,
                 csp_config: dict):
        """

        :param trough_config: dict, with keys ('system_capacity_kw', 'solar_multiple', 'tes_hours')
        """
        required_keys = ['system_capacity_kw', 'solar_multiple', 'tes_hours']
        if all(key not in csp_config.keys() for key in required_keys):
            raise ValueError

        self.name = name
        self.site = site

        self._financial_model = financial_model
        self._layout = None
        self._dispatch = CspDispatch

        # TODO: Site should have dispatch factors consistent across all models




        # Initialize ssc and get weather data
        self.ssc = ssc_wrap(
            wrapper='pyssc',  # ['pyssc' | 'pysam']
            tech_name=tech_name,  # ['tcsmolten_salt' | 'trough_physical]
            financial_name=None,
            defaults_name=None)  # ['MSPTSingleOwner' | 'PhysicalTroughSingleOwner']  NOTE: not used for pyssc
        self.initialize_params(keep_eta_flux_maps=False)
        self.year_weather_df = self.tmy3_to_df()  # read entire weather file

        # TODO: move to tower_source
        if self.ssc.tech_name == 'tcsmolten_salt':
            # Calculate flux and eta maps for all simulations
            start_datetime = datetime.datetime(1900, 1, 1, 0, 0, 0)  # start of first timestep
            self.set_weather(self.year_weather_df, start_datetime, start_datetime)  # only one weather timestep is needed
            self.set_flux_eta_maps(self.simulate_flux_eta_maps())

    def param_file_paths(self, relative_path):
        cwd = os.path.dirname(os.path.abspath(__file__))
        data_path = os.path.join(cwd, relative_path)
        for key in self.param_files.keys():
            filename = self.param_files[key]
            self.param_files[key] = os.path.join(data_path, filename)

    def initialize_params(self, keep_eta_flux_maps=False):
        if self.ssc.tech_name == 'tcsmolten_salt' and keep_eta_flux_maps == True:
            flux_eta_maps = {
                'eta_map': self.ssc.get('eta_map'),
                'flux_maps': self.ssc.get('flux_maps'),
                'A_sf_in': self.ssc.get('A_sf_in')}
            self.set_params_from_files()
            self.set_flux_eta_maps(flux_eta_maps)
        else:
            self.set_params_from_files()

        self.ssc.set({'time_steps_per_hour': 1})  # FIXME: defaults to 60
        n_steps_year = int(8760 * self.ssc.get('time_steps_per_hour'))
        self.ssc.set({'sf_adjust:hourly': n_steps_year * [0]})

    def tmy3_to_df(self):
        # if not isinstance(self.site.solar_resource.filename, str) or not os.path.isfile(self.site.solar_resource.filename):
        #     raise Exception('Tmy3 file not found')

        # NOTE: be careful of leading spaces in the column names, they are hard to catch and break the parser
        df = pd.read_csv(self.site.solar_resource.filename, sep=',', skiprows=2, header=0)
        date_cols = ['Year', 'Month', 'Day', 'Hour', 'Minute']
        df.index = pd.to_datetime(df[date_cols])
        df.index.name = 'datetime'
        df.drop(date_cols, axis=1, inplace=True)

        df.index = df.index.map(lambda t: t.replace(year=df.index[0].year))  # normalize all years to that of 1/1
        df = df[df.columns.drop(list(df.filter(regex='Unnamed')))]  # drop unnamed columns (which are empty)
        timestep = df.index[1] - df.index[0]
        if timestep == datetime.timedelta(hours=1) and df.index[0].minute == 30:
            df.index = df.index.map(
                lambda t: t.replace(minute=0))  # make minute convention 0 instead of 30 in hourly files

        def get_weatherfile_location(tmy3_path):
            df_meta = pd.read_csv(tmy3_path, sep=',', header=0, nrows=1)
            return {
                'latitude': float(df_meta['Latitude'][0]),
                'longitude': float(df_meta['Longitude'][0]),
                'timezone': int(df_meta['Time Zone'][0]),
                'elevation': float(df_meta['Elevation'][0])
            }

        location = get_weatherfile_location(self.site.solar_resource.filename)
        df.attrs.update(location)
        return df

    def set_params_from_files(self):
        with open(self.param_files['tech_model_params_path'], 'r') as f:
            ssc_params = rapidjson.load(f)
        self.ssc.set(ssc_params)

        # NOTE: Don't set if passing weather data in via solar_resource_data
        # ssc.set({'solar_resource_file': param_files['solar_resource_file_path']})

        dispatch_factors_ts = np.array(pd.read_csv(self.param_files['dispatch_factors_ts_path']))
        self.ssc.set({'dispatch_factors_ts': dispatch_factors_ts})

        ud_ind_od = np.array(pd.read_csv(self.param_files['ud_ind_od_path']))
        self.ssc.set({'ud_ind_od': ud_ind_od})

        wlim_series = np.array(pd.read_csv(self.param_files['wlim_series_path']))
        self.ssc.set({'wlim_series': wlim_series})

        if self.ssc.tech_name == 'tcsmolten_salt':
            heliostat_layout = np.genfromtxt(self.param_files['helio_positions_path'], delimiter=',')
            N_hel = heliostat_layout.shape[0]
            helio_positions = [heliostat_layout[j, 0:2].tolist() for j in range(N_hel)]
            self.ssc.set({'helio_positions': helio_positions})

    def set_weather(self, weather_df, start_datetime, end_datetime):
        weather_timedelta = weather_df.index[1] - weather_df.index[0]
        weather_time_steps_per_hour = int(1 / (weather_timedelta.total_seconds() / 3600))
        ssc_time_steps_per_hour = self.ssc.get('time_steps_per_hour')
        if weather_time_steps_per_hour != ssc_time_steps_per_hour:
            raise Exception('Configured time_steps_per_hour ({x}) is not that of weather file ({y})'.format(
                x=ssc_time_steps_per_hour, y=weather_time_steps_per_hour))

        weather_year = weather_df.index[0].year
        if start_datetime.year != weather_year:
            print('Replacing start and end years ({x}) with weather file\'s ({y}).'.format(
                x=start_datetime.year, y=weather_year))
            start_datetime = start_datetime.replace(year=weather_year)
            end_datetime = end_datetime.replace(year=weather_year)

        if end_datetime <= start_datetime:
            end_datetime = start_datetime + weather_timedelta
        weather_df_part = weather_df[start_datetime:(
                    end_datetime - weather_timedelta)]  # times in weather file are the start (or middle) of timestep

        def weather_df_to_ssc_table(weather_df):
            rename_from_to = {
                'Tdry': 'Temperature',
                'Tdew': 'Dew Point',
                'RH': 'Relative Humidity',
                'Pres': 'Pressure',
                'Wspd': 'Wind Speed',
                'Wdir': 'Wind Direction'
            }
            weather_df = weather_df.rename(columns=rename_from_to)

            solar_resource_data = {}
            solar_resource_data['tz'] = weather_df.attrs['timezone']
            solar_resource_data['elev'] = weather_df.attrs['elevation']
            solar_resource_data['lat'] = weather_df.attrs['latitude']
            solar_resource_data['lon'] = weather_df.attrs['longitude']
            solar_resource_data['year'] = list(weather_df.index.year)
            solar_resource_data['month'] = list(weather_df.index.month)
            solar_resource_data['day'] = list(weather_df.index.day)
            solar_resource_data['hour'] = list(weather_df.index.hour)
            solar_resource_data['minute'] = list(weather_df.index.minute)
            solar_resource_data['dn'] = list(weather_df['DNI'])
            solar_resource_data['df'] = list(weather_df['DHI'])
            solar_resource_data['gh'] = list(weather_df['GHI'])
            solar_resource_data['wspd'] = list(weather_df['Wind Speed'])
            solar_resource_data['tdry'] = list(weather_df['Temperature'])
            solar_resource_data['pres'] = list(weather_df['Pressure'])
            solar_resource_data['tdew'] = list(weather_df['Dew Point'])

            def pad_solar_resource_data(solar_resource_data):
                datetime_start = datetime.datetime(
                    year=solar_resource_data['year'][0],
                    month=solar_resource_data['month'][0],
                    day=solar_resource_data['day'][0],
                    hour=solar_resource_data['hour'][0],
                    minute=solar_resource_data['minute'][0])
                n = len(solar_resource_data['dn'])
                if n < 2:
                    timestep = datetime.timedelta(hours=1)  # assume 1 so minimum of 8760 results
                else:
                    datetime_second_time = datetime.datetime(
                        year=solar_resource_data['year'][1],
                        month=solar_resource_data['month'][1],
                        day=solar_resource_data['day'][1],
                        hour=solar_resource_data['hour'][1],
                        minute=solar_resource_data['minute'][1])
                    timestep = datetime_second_time - datetime_start
                steps_per_hour = int(3600 / timestep.seconds)
                # Substitute a non-leap year (2009) to keep multiple of 8760 assumption:
                i0 = int((datetime_start.replace(year=2009) - datetime.datetime(2009, 1, 1, 0, 0,
                                                                                0)).total_seconds() / timestep.seconds)
                diff = 8760 * steps_per_hour - n
                front_padding = [0] * i0
                back_padding = [0] * (diff - i0)

                if diff > 0:
                    for k in solar_resource_data:
                        if isinstance(solar_resource_data[k], list):
                            solar_resource_data[k] = front_padding + solar_resource_data[k] + back_padding
                    return solar_resource_data

            solar_resource_data = pad_solar_resource_data(solar_resource_data)
            return solar_resource_data

        self.ssc.set({'solar_resource_data': weather_df_to_ssc_table(weather_df_part)})

    def simulate_flux_eta_maps(self):
        print('Simulating flux and eta maps ...')
        self.initialize_params(keep_eta_flux_maps=False)
        self.ssc.set({'time_start': 0})
        self.ssc.set({'time_stop': 0})
        self.ssc.set({'field_model_type': 2})  # generate flux and eta maps but don't optimize field or tower
        original_values = {k: self.ssc.get(k) for k in
                           ['is_dispatch_targets', 'rec_clearsky_model', 'time_steps_per_hour', 'sf_adjust:hourly', ]}
        self.ssc.set({'is_dispatch_targets': False, 'rec_clearsky_model': 1, 'time_steps_per_hour': 1,
                 'sf_adjust:hourly': [0.0 for j in range(
                     8760)]})  # set so unneeded dispatch targets and clearsky DNI are not required
        self.ssc.set({'sf_adjust:hourly': [0.0 for j in range(8760)]})
        tech_outputs = self.ssc.execute()
        print('Finished simulating flux and eta maps ...')
        self.ssc.set(original_values)
        eta_map = tech_outputs["eta_map_out"]
        flux_maps = [r[2:] for r in tech_outputs['flux_maps_for_import']]  # don't include first two columns
        A_sf_in = tech_outputs["A_sf"]
        flux_eta_maps = {'eta_map': eta_map, 'flux_maps': flux_maps, 'A_sf_in': A_sf_in}
        return flux_eta_maps

    def set_flux_eta_maps(self, flux_eta_maps):
        self.ssc.set(flux_eta_maps)  # set flux maps etc. so they don't have to be recalculated
        self.ssc.set({'field_model_type': 3})  # use the provided flux and eta map inputs
        self.ssc.set({'eta_map_aod_format': False})  #

    @staticmethod
    def seconds_since_newyear(dt):
        # Substitute a non-leap year (2009) to keep multiple of 8760 assumption:
        newyear = datetime.datetime(2009, 1, 1, 0, 0, 0, 0)
        time_diff = dt.replace(year=2009) - newyear
        return int(time_diff.total_seconds())

    # TODO: overwrite all setters and getters inherited by PowerSource