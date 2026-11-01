/*
 * EnergyBalance.cpp
 *
 * Today's energy flows between grid, solar, battery and house for the devices selected in the Energy Dashboard
 * settings, and the self-sufficiency derived from them.
 *
 * The daily totals of the meters cannot tell whether the battery was charged from solar or from the grid, so the
 * flows are split per short log interval (normally 5 minutes): solar goes to the house first, then to the battery,
 * then to the grid; the house takes battery energy before grid energy. The battery keeps track of how much of the
 * energy it holds came from the grid, and energy it later delivers to the house counts as grid energy for that part.
 */

#include "stdafx.h"
#include "EnergyBalance.h"

#include "Helper.h"
#include "SQLHelper.h"
#include "WebServerHelper.h"
#include "json_helper.h"
#include "localtime_r.h"
#include "mainworker.h"

#define __STDC_FORMAT_MACROS
#include <inttypes.h>
#include <algorithm>
#include <map>
#include <mutex>

extern http::server::CWebServerHelper m_webservers;

namespace
{
	// A step larger than this within one interval is a counter reset or replacement, not energy
	constexpr double MAX_INTERVAL_KWH = 50.0;
	constexpr int CACHE_SECONDS = 60;

	std::mutex g_cacheMutex;
	time_t g_cacheTime = 0;
	_tEnergyBalance g_cache;

	// Cumulative counter in kWh per interval key ("YYYY-MM-DD HH:MM", floored to the short log interval)
	typedef std::map<std::string, double> _tCounterRows;

	std::string IntervalKey(const std::string &sDate, const int interval)
	{
		if (sDate.size() < 16)
			return "";
		int minute = atoi(sDate.substr(14, 2).c_str());
		if (interval > 1)
			minute -= minute % interval;
		return sDate.substr(0, 14) + std_format("%02d", minute);
	}

	bool GetDeviceType(const int idx, unsigned char &dType, unsigned char &dSubType, _eMeterType &metertype, float &divider)
	{
		auto result = m_sql.safe_query("SELECT Type, SubType, SwitchType, AddjValue2 FROM DeviceStatus WHERE (ID == %d)", idx);
		if (result.empty())
			return false;
		dType = static_cast<unsigned char>(atoi(result[0][0].c_str()));
		dSubType = static_cast<unsigned char>(atoi(result[0][1].c_str()));
		metertype = static_cast<_eMeterType>(atoi(result[0][2].c_str()));
		if ((dType == pTypeP1Power) || (dType == pTypeENERGY) || (dType == pTypePOWER) || (dType == pTypeCURRENTENERGY)
			|| ((dType == pTypeGeneral) && (dSubType == sTypeKwh)))
			metertype = MTYPE_ENERGY;
		divider = m_sql.GetCounterDivider(int(metertype), int(dType), static_cast<float>(atof(result[0][3].c_str())));
		if (divider == 0)
			divider = 1000.0F;
		return true;
	}

	// Reads the short log of an energy counter since szStart. For a P1 meter bDelivery selects the returned
	// energy instead of the used energy.
	void ReadCounterRows(const int idx, const bool bDelivery, const std::string &szStart, const int interval, _tCounterRows &rows)
	{
		unsigned char dType, dSubType;
		_eMeterType metertype;
		float divider;
		if (!GetDeviceType(idx, dType, dSubType, metertype, divider))
			return;

		std::vector<std::vector<std::string>> result;
		if (dType == pTypeP1Power)
		{
			if (bDelivery)
				result = m_sql.safe_query("SELECT Value2 + Value6, Date FROM MultiMeter WHERE (DeviceRowID == %d) AND (Date >= '%q') ORDER BY Date ASC", idx, szStart.c_str());
			else
				result = m_sql.safe_query("SELECT Value1 + Value5, Date FROM MultiMeter WHERE (DeviceRowID == %d) AND (Date >= '%q') ORDER BY Date ASC", idx, szStart.c_str());
			divider = 1000.0F;
		}
		else
			result = m_sql.safe_query("SELECT Value, Date FROM Meter WHERE (DeviceRowID == %d) AND (Date >= '%q') ORDER BY Date ASC", idx, szStart.c_str());

		for (const auto &sd : result)
		{
			std::string key = IntervalKey(sd[1], interval);
			if (!key.empty())
				rows[key] = atof(sd[0].c_str()) / divider;
		}
	}

	// Today's counter of a device as the device list shows it (CounterToday, or CounterDelivToday for the returned energy)
	double GetCounterToday(const int idx, const bool bDelivery)
	{
		Json::Value devices;
		m_webservers.GetJSonDevices(devices, "", "", "", std::to_string(idx), "", "", false, true, false, 0, "", "");
		if (!devices.isMember("result") || devices["result"].empty())
			return 0;
		const Json::Value &dev = devices["result"][0];
		const char *szField = bDelivery ? "CounterDelivToday" : "CounterToday";
		if (!dev.isMember(szField))
			return 0;
		return std::max(0.0, atof(dev[szField].asString().c_str()));
	}

	void CalculateDaily(_tEnergyBalance &eb, const int idP1, const int idSolar, const int idBatIn, const int idBatOut)
	{
		eb.Method = "daily";
		eb.Import = GetCounterToday(idP1, false);
		eb.Export = GetCounterToday(idP1, true);
		eb.Solar = (idSolar != -1) ? GetCounterToday(idSolar, false) : 0;
		eb.BatteryIn = (idBatIn != -1) ? GetCounterToday(idBatIn, false) : 0;
		eb.BatteryOut = (idBatOut != -1) ? GetCounterToday(idBatOut, false) : 0;
		eb.House = std::max(0.0, eb.Import + eb.Solar - eb.Export - eb.BatteryIn + eb.BatteryOut);
		if (eb.House > 0)
			eb.SelfSufficiency = std::min(100.0, std::max(0.0, (1.0 - eb.Import / eb.House) * 100.0));
	}

	void Calculate(_tEnergyBalance &eb)
	{
		std::string szESettings;
		Json::Value esettings;
		if (!m_sql.GetPreferencesVar("ESettings", szESettings) || !ParseJSon(szESettings, esettings))
		{
			eb.Method = "none";
			return;
		}
		auto getIdx = [&esettings](const char *szName) {
			return (esettings.isMember(szName) && esettings[szName].isInt() && esettings[szName].asInt() > 0) ? esettings[szName].asInt() : -1;
		};
		const int idP1 = getIdx("idP1");
		const int idSolar = getIdx("idSolar");
		const int idBatIn = getIdx("idBatteryEnergyIn");
		const int idBatOut = getIdx("idBatteryEnergyOut");
		if (idP1 == -1)
		{
			eb.Method = "none";
			return;
		}

		time_t now = mytime(nullptr);
		struct tm ltime;
		localtime_r(&now, &ltime);
		const std::string szToday = std_format("%04d-%02d-%02d 00:00", ltime.tm_year + 1900, ltime.tm_mon + 1, ltime.tm_mday);
		// The day before warms up the share of grid energy in the battery
		time_t tYesterday = now - 24 * 3600;
		struct tm ytime;
		localtime_r(&tYesterday, &ytime);
		const std::string szStart = std_format("%04d-%02d-%02d 00:00:00", ytime.tm_year + 1900, ytime.tm_mon + 1, ytime.tm_mday);

		const int interval = std::max(1, m_sql.m_ShortLogInterval);
		_tCounterRows rowsImport, rowsExport, rowsSolar, rowsBatIn, rowsBatOut;
		ReadCounterRows(idP1, false, szStart, interval, rowsImport);
		ReadCounterRows(idP1, true, szStart, interval, rowsExport);
		if (idSolar != -1)
			ReadCounterRows(idSolar, true, szStart, interval, rowsSolar);
		if (idBatIn != -1)
			ReadCounterRows(idBatIn, false, szStart, interval, rowsBatIn);
		if (idBatOut != -1)
			ReadCounterRows(idBatOut, false, szStart, interval, rowsBatOut);

		if (rowsImport.empty() || (rowsImport.rbegin()->first < szToday))
		{
			CalculateDaily(eb, idP1, idSolar, idBatIn, idBatOut);
			return;
		}

		std::vector<std::string> keys;
		for (const auto *rows : { &rowsImport, &rowsExport, &rowsSolar, &rowsBatIn, &rowsBatOut })
			for (const auto &itt : *rows)
				keys.push_back(itt.first);
		std::sort(keys.begin(), keys.end());
		keys.erase(std::unique(keys.begin(), keys.end()), keys.end());

		// Last known cumulative value per counter, the step since then is the energy of the interval
		struct _tCounterState
		{
			const _tCounterRows *rows;
			bool bHave = false;
			double last = 0;

			double Step(const std::string &key)
			{
				auto itt = rows->find(key);
				if (itt == rows->end())
					return 0;
				double step = 0;
				if (bHave)
				{
					step = itt->second - last;
					if ((step < 0) || (step > MAX_INTERVAL_KWH))
						step = 0;
				}
				bHave = true;
				last = itt->second;
				return step;
			}
		};
		_tCounterState cImport{ &rowsImport }, cExport{ &rowsExport }, cSolar{ &rowsSolar }, cBatIn{ &rowsBatIn }, cBatOut{ &rowsBatOut };

		double poolEnergy = 0;		// energy in the battery
		double poolGrid = 0;		// the part of it that came from the grid
		double gridToHouseTotal = 0;	// grid energy used by the house, directly or through the battery

		for (const auto &key : keys)
		{
			const double I = cImport.Step(key);
			const double E = cExport.Step(key);
			const double S = cSolar.Step(key);
			const double C = cBatIn.Step(key);
			const double D = cBatOut.Step(key);

			const double used = std::max(0.0, I + S + D - E - C);
			const double s2h = std::min(S, used);
			const double b2h = std::min(D, used - s2h);
			const double g2h = used - s2h - b2h;
			const double sRest = S - s2h;
			const double s2b = std::min(sRest, C);
			const double g2b = C - s2b;
			const double s2g = sRest - s2b;
			const double b2g = D - b2h;

			const double share = (poolEnergy > 0) ? std::min(1.0, poolGrid / poolEnergy) : 0.0;
			const double b2hGrid = b2h * share;
			poolGrid = std::max(0.0, poolGrid - D * share) + g2b;
			poolEnergy = std::max(0.0, poolEnergy - D) + s2b + g2b;

			// The interval that ends at midnight still belongs to yesterday
			if (key <= szToday)
				continue;

			eb.Intervals++;
			eb.Import += I;
			eb.Export += E;
			eb.Solar += S;
			eb.BatteryIn += C;
			eb.BatteryOut += D;
			eb.House += used;
			eb.SolarToHouse += s2h;
			eb.BatteryToHouse += b2h;
			eb.GridToHouse += g2h;
			eb.SolarToBattery += s2b;
			eb.GridToBattery += g2b;
			eb.SolarToGrid += s2g;
			eb.BatteryToGrid += b2g;
			gridToHouseTotal += g2h + b2hGrid;
		}

		eb.Method = "5min";
		if (eb.House > 0)
			eb.SelfSufficiency = std::min(100.0, std::max(0.0, (1.0 - gridToHouseTotal / eb.House) * 100.0));
	}
} // namespace

bool CalculateEnergyBalanceToday(_tEnergyBalance &eb)
{
	std::lock_guard<std::mutex> l(g_cacheMutex);
	time_t now = mytime(nullptr);
	if ((g_cacheTime != 0) && (now - g_cacheTime < CACHE_SECONDS) && (now >= g_cacheTime))
	{
		eb = g_cache;
		return eb.Method != "none";
	}
	g_cache = _tEnergyBalance();
	Calculate(g_cache);
	g_cacheTime = now;
	eb = g_cache;
	return eb.Method != "none";
}
