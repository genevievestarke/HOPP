import os
from pathlib import Path
from dotenv import load_dotenv

from hybrid.sites import SiteInfo, flatirons_site
from hybrid.hybrid_simulation import HybridSimulation
from hybrid.log import hybrid_logger as logger
from hybrid.keys import set_nrel_key_dot_env


# Set API key
set_nrel_key_dot_env()

# Set wind, solar, and interconnection capacities (in MW)
solar_size_mw = 20
wind_size_mw = 20
interconnection_size_mw = 20

technologies = {'solar': {
                    'system_capacity_kw': solar_size_mw * 1000
                },
                'wind': {
                    'num_turbines': 10,
                    'turbine_rating_kw': 2000
                },
                'grid': interconnection_size_mw}

# Get resource
lat = flatirons_site['lat']
lon = flatirons_site['lon']
prices_file = Path(__file__).parent.parent / "resource_files" / "grid" / "pricing-data-2015-IronMtn-002_factors.csv"
site = SiteInfo(flatirons_site, grid_resource_file=prices_file)

# Create model
hybrid_plant = HybridSimulation(technologies, site, interconnect_kw=interconnection_size_mw * 1000)

hybrid_plant.solar.system_capacity_kw = solar_size_mw * 1000
hybrid_plant.wind.system_capacity_by_num_turbines(wind_size_mw * 1000)
hybrid_plant.ppa_price = 0.1
hybrid_plant.simulate(25)

# Save the outputs
annual_energies = hybrid_plant.annual_energies
wind_plus_solar_npv = hybrid_plant.net_present_values.wind + hybrid_plant.net_present_values.solar
npvs = hybrid_plant.net_present_values

wind_installed_cost = hybrid_plant.wind._financial_model.SystemCosts.total_installed_cost
solar_installed_cost = hybrid_plant.solar._financial_model.SystemCosts.total_installed_cost
hybrid_installed_cost = hybrid_plant.grid._financial_model.SystemCosts.total_installed_cost

print("Wind Installed Cost: {}".format(wind_installed_cost))
print("Solar Installed Cost: {}".format(solar_installed_cost))
print("Hybrid Installed Cost: {}".format(hybrid_installed_cost))
print("Wind NPV: {}".format(hybrid_plant.net_present_values.wind))
print("Solar NPV: {}".format(hybrid_plant.net_present_values.solar))
print("Hybrid NPV: {}".format(hybrid_plant.net_present_values.hybrid))
print("Wind + Solar Expected NPV: {}".format(wind_plus_solar_npv))


print(annual_energies)
print(npvs)
