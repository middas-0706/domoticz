#pragma once

#include <string>

// Today's energy flows of the devices selected in the Energy Dashboard settings, in kWh
struct _tEnergyBalance
{
	std::string Method;		// "5min" (short log intervals), "daily" (today's counters) or "none" (no grid meter selected)
	int Intervals = 0;

	double Import = 0;
	double Export = 0;
	double Solar = 0;
	double BatteryIn = 0;
	double BatteryOut = 0;
	double House = 0;

	double SolarToHouse = 0;
	double BatteryToHouse = 0;
	double GridToHouse = 0;
	double SolarToBattery = 0;
	double GridToBattery = 0;
	double SolarToGrid = 0;
	double BatteryToGrid = 0;

	double SelfSufficiency = -1;	// percentage of the house consumption that did not come from the grid, -1 when unknown
};

bool CalculateEnergyBalanceToday(_tEnergyBalance &eb);
