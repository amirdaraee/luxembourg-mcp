"""Offline parsing tests for the tools added on top of the original 28."""

import unittest
from datetime import datetime, timedelta, timezone

from luxembourg_mcp.http import UpstreamError
from luxembourg_mcp.providers import LuxembourgData


class ScriptedHttp:
    """Serves queued responses and records the URLs and headers each call used."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, url, headers=None):
        self.calls.append((url, headers))
        if not self.responses:
            raise AssertionError(f"no scripted response left for {url}")
        return self.responses.pop(0)

    def get_json(self, url, headers=None, **kwargs):
        return self._next(url, headers)

    def get_json_value(self, url, headers=None, **kwargs):
        return self._next(url, headers)

    def get_bytes(self, url, headers=None, **kwargs):
        value = self._next(url, headers)
        return value, "utf-8"


def dataset(slug, resources):
    return {
        "slug": slug,
        "page": f"https://data.public.lu/en/datasets/{slug}/",
        "resources": [{"format": fmt, "url": url, "title": title, "last_modified": modified}
                      for fmt, url, title, modified in resources],
    }


ALERT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>LU-Alert.1</identifier><sender>[ALVA]</sender><sent>2026-09-16T17:50:11+02:00</sent>
  <status>Actual</status><msgType>Alert</msgType><scope>Public</scope>
  <info><language>fr-FR</language><category>Health</category><event>Rappel alimentaire</event>
    <urgency>Unknown</urgency><severity>Unknown</severity><certainty>Unknown</certainty>
    <expires>{expires}</expires><headline>Rappel FR</headline>
    <description>&lt;p&gt;&lt;strong&gt;Motif :&lt;/strong&gt; Salmonella&lt;/p&gt;&lt;p&gt;Ne pas consommer.&lt;/p&gt;</description>
    <area><areaDesc>Grand-Duche du Luxembourg</areaDesc></area></info>
  <info><language>en-GB</language><category>Health</category><event>Food recall</event>
    <urgency>Unknown</urgency><severity>Unknown</severity><certainty>Unknown</certainty>
    <expires>{expires}</expires><headline>Recall EN</headline>
    <description>&lt;p&gt;Do not eat.&lt;/p&gt;</description>
    <area><areaDesc>Luxembourg</areaDesc></area></info>
</alert>"""


def alert_xml(days_from_now):
    expires = (datetime.now(timezone.utc) + timedelta(days=days_from_now)).isoformat()
    return ALERT_XML.format(expires=expires).encode("utf-8")


# Two series as ENTSO-E publishes them; series "1" is the full curve, and position 3 is
# missing so curveType A03 carry-forward has to fill it.
ELECTRICITY_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries><mRID>1</mRID>
    <classificationSequence_AttributeInstanceComponent.position>2</classificationSequence_AttributeInstanceComponent.position>
    <currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <Period><timeInterval><start>2026-09-16T22:00Z</start><end>2026-09-17T22:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>999.00</price.amount></Point>
    </Period></TimeSeries>
  <TimeSeries><mRID>2</mRID>
    <classificationSequence_AttributeInstanceComponent.position>1</classificationSequence_AttributeInstanceComponent.position>
    <currency_Unit.name>EUR</currency_Unit.name><price_Measure_Unit.name>MWH</price_Measure_Unit.name>
    <Period><timeInterval><start>2026-09-16T22:00Z</start><end>2026-09-17T22:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>100.50</price.amount></Point>
      <Point><position>2</position><price.amount>80.25</price.amount></Point>
      <Point><position>4</position><price.amount>120.75</price.amount></Point>
    </Period></TimeSeries>
</Publication_MarketDocument>"""


class ForecastTests(unittest.TestCase):
    payload = {
        "city": {"name": "Ettelbruck", "canton": "Diekirch"},
        "forecast": {
            "current": {"date": "2026-09-17T09:42:00", "icon": {"name": "High clouds"}, "wind": {"direction": "SO", "speed": "05-10", "gusts": None}, "rain": "0", "snow": "0", "temperature": {"temperature": 12}},
            "hourly": [{"date": "2026-09-17T10:00:00", "icon": {"name": "Sunny"}, "wind": {}, "temperature": {"temperature": [9, 11]}}],
            "daily": [{"date": "2026-09-18T00:00:00", "icon": {"name": "Cloudy"}, "wind": {}, "temperatureMin": {"temperature": 11}, "temperatureMax": {"temperature": 20}, "sunshine": 6, "uvIndex": 4}],
        },
        "vigilances": [{"id": 1}],
        "ephemeris": {"sunrise": "07:15", "sunset": "19:44", "uvIndex": 4},
    }

    def test_shapes_current_hourly_and_daily(self):
        http = ScriptedHttp([self.payload])
        result = LuxembourgData(http).get_weather_forecast(latitude=49.85, longitude=6.1, language="de")
        self.assertEqual(result["place"], "Ettelbruck")
        self.assertEqual(result["current"]["temperature_c"], 12)
        self.assertEqual(result["daily"][0]["temperature_max_c"], 20)
        self.assertEqual(result["daily"][0]["uv_index"], 4)
        self.assertEqual(result["hourly"][0]["temperature_c"], [9, 11])
        self.assertEqual((result["sunset"], result["active_warnings"]), ("19:44", 1))
        self.assertIn("langcode=de", http.calls[0][0])

    def test_rejects_remote_coordinates_and_unknown_language(self):
        data = LuxembourgData(ScriptedHttp([]))
        with self.assertRaises(ValueError):
            data.get_weather_forecast(latitude=0, longitude=0)
        with self.assertRaises(ValueError):
            data.get_weather_forecast(language="es")


class FuelPriceTests(unittest.TestCase):
    csv = "﻿Intervalle,Essence_E10,Diesel_B7,Hydrogene,LPG,Electricie_Fournisseur,Recharge_AC,Recharge_DC\nJan-26,1.424,1.398,13.85,0.689,0.251,0.52,0.672\nFeb-26,1.457,1.439,13.85,0.709,0.251,0.52,0.672\n".encode("utf-8")

    def test_returns_newest_month_first(self):
        http = ScriptedHttp([dataset("fuel", [("csv", "https://download.data.public.lu/fuel.csv", "prices", "2026-09-09")]), self.csv])
        result = LuxembourgData(http).get_fuel_prices(months=1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["prices"][0]["month"], "Feb-26")
        self.assertEqual(result["prices"][0]["diesel_b7_eur_per_litre"], 1.439)
        self.assertEqual(result["prices"][0]["home_electricity_eur_per_kwh"], 0.251)


class PublicAlertTests(unittest.TestCase):
    @staticmethod
    def metadata():
        return dataset("alertes-du-systeme-lu-alert", [
            ("xml", "https://download.data.public.lu/new.xml", "new", "2026-09-16T15:55:07+00:00"),
            ("xml", "https://download.data.public.lu/old.xml", "old", "2026-09-10T10:00:00+00:00"),
        ])

    def test_picks_requested_language_and_strips_html(self):
        http = ScriptedHttp([self.metadata(), alert_xml(2), alert_xml(2)])
        result = LuxembourgData(http).get_public_alerts(limit=1, language="en")
        alert = result["alerts"][0]
        self.assertEqual((alert["language"], alert["event"]), ("en-GB", "Food recall"))
        self.assertEqual(alert["description"], "Do not eat.")
        self.assertEqual(alert["areas"], ["Luxembourg"])

    def test_french_description_keeps_text_without_tags(self):
        http = ScriptedHttp([self.metadata(), alert_xml(2)])
        alert = LuxembourgData(http).get_public_alerts(limit=1, language="fr")["alerts"][0]
        self.assertEqual(alert["language"], "fr-FR")
        self.assertNotIn("<", alert["description"])
        self.assertIn("Salmonella", alert["description"])

    def test_active_only_skips_expired_alerts(self):
        http = ScriptedHttp([self.metadata(), alert_xml(-1), alert_xml(-2)])
        self.assertEqual(LuxembourgData(http).get_public_alerts(limit=2)["count"], 0)
        http = ScriptedHttp([self.metadata(), alert_xml(-1), alert_xml(1)])
        self.assertEqual(LuxembourgData(http).get_public_alerts(limit=2)["count"], 1)


class CommuneTests(unittest.TestCase):
    leaders_csv = ("﻿COM_CODE,COM_LABEL,MAN_LABEL,ELU_LAST_NAME,ELU_FIRST_NAME,ELU_SEX,COAC_START_DATE,COAC_END_DATE\n"
                   "C004,Bech,Bourgmestre,Goeres,Jill,F,04/07/2023,05/02/2026\n"
                   "C004,Bech,Bourgmestre,Weber,Marc,M,06/02/2026,\n"
                   "C004,Bech,Échevin,Kraus,Anne,F,23/10/2023,\n"
                   "C003,Beaufort,Bourgmestre,Nosbusch,Jean-Luc,M,27/10/2023,\n").encode("utf-8")
    # The register ships latin-1, which is what the shared CSV decoder has to cope with.
    population_csv = ("COMMUNE_CODE,COMMUNE_NOM,FEMMES_MINEURES,HOMMES_MINEURS,FEMMES_MAJEURES,HOMMES_MAJEURS\n"
                      "1101,Beaufort,309,303,1270,1264\n"
                      "0605,Bertrange,1,2,3,4\n").encode("latin-1")

    def test_leaders_exclude_finished_mandates(self):
        http = ScriptedHttp([dataset("college", [("csv", "https://download.data.public.lu/college.csv", "college", "2026-04-01")]), self.leaders_csv])
        result = LuxembourgData(http).get_commune_leaders(commune="Bech")
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["leaders"][0]["role"], "Bourgmestre")
        self.assertEqual(result["leaders"][0]["last_name"], "Weber")
        self.assertEqual(result["leaders"][0]["since"], "2026-02-06")

    def test_unknown_commune_is_a_value_error(self):
        http = ScriptedHttp([dataset("college", [("csv", "https://download.data.public.lu/college.csv", "college", "2026-04-01")]), self.leaders_csv])
        with self.assertRaises(ValueError):
            LuxembourgData(http).get_commune_leaders(commune="Atlantis")

    def test_population_totals_and_latest_resource(self):
        metadata = dataset("rnpp", [
            ("csv", "https://download.data.public.lu/old.csv", "01-04-2026", "2026-04-06T11:52:21+00:00"),
            ("csv", "https://download.data.public.lu/new.csv", "01-07-2026", "2026-07-06T11:52:21+00:00"),
            ("xml", "https://download.data.public.lu/new.xml", "01-07-2026", "2026-07-06T11:52:21+00:00"),
        ])
        http = ScriptedHttp([metadata, self.population_csv])
        result = LuxembourgData(http).get_commune_population(commune="Bertrange")
        self.assertEqual(http.calls[1][0], "https://download.data.public.lu/new.csv")
        commune = result["communes"][0]
        self.assertEqual((commune["population"], commune["adults"], commune["minors"]), (10, 7, 3))
        self.assertEqual((commune["female"], commune["male"]), (4, 6))


class PharmacyTests(unittest.TestCase):
    csv = ('﻿"Pharmacie de Garde";Adresse;Téléphone;Date\n'
           '"Pharmacie A";"1 rue St. Antoine L-9205 DIEKIRCH";"+352 80 35 85";2026-09-17\n'
           '"Pharmacie B";"28 rue Victor Hugo L-4140 ESCH-SUR-ALZETTE";"+352 55 41 09";2026-09-17\n'
           '"Pharmacie C";"5 Grand-Rue L-1660 LUXEMBOURG";"+352 22 11 33";2026-09-18\n').encode("utf-8")

    def test_filters_by_date_and_locality(self):
        http = ScriptedHttp([self.csv])
        result = LuxembourgData(http).get_pharmacies_on_duty(date="2026-09-17", locality="esch")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["pharmacies"][0]["name"], "Pharmacie B")
        self.assertEqual(result["pharmacies"][0]["phone"], "+352 55 41 09")
        self.assertEqual(result["covered_dates"], ["2026-09-17", "2026-09-18"])

    def test_date_outside_the_window_explains_the_range(self):
        http = ScriptedHttp([self.csv])
        with self.assertRaisesRegex(ValueError, "2026-09-17 to 2026-09-18"):
            LuxembourgData(http).get_pharmacies_on_duty(date="2030-01-01")

    def test_malformed_date_is_rejected_before_fetching(self):
        with self.assertRaises(ValueError):
            LuxembourgData(ScriptedHttp([])).get_pharmacies_on_duty(date="17/09/2026")


class ElectricityTests(unittest.TestCase):
    def result(self):
        metadata = dataset("electricity", [("xml", "https://download.data.public.lu/day.xml", "Day-ahead prices - 17 September 2026", "2026-09-17T07:50:39+00:00")])
        return LuxembourgData(ScriptedHttp([metadata, ELECTRICITY_XML])).get_electricity_prices()

    def test_uses_the_primary_series_and_fills_sparse_points(self):
        result = self.result()
        self.assertEqual(result["count"], 4)
        self.assertEqual([item["eur_per_mwh"] for item in result["prices"]], [100.5, 80.25, 80.25, 120.75])
        self.assertNotIn(999.0, [item["eur_per_mwh"] for item in result["prices"]])

    def test_timestamps_follow_the_resolution(self):
        prices = self.result()["prices"]
        self.assertEqual(prices[0]["start"], "2026-09-16T22:00:00Z")
        self.assertEqual(prices[1]["start"], "2026-09-16T22:15:00Z")

    def test_reports_cheapest_dearest_and_average(self):
        result = self.result()
        self.assertEqual(result["cheapest"]["eur_per_mwh"], 80.25)
        self.assertEqual(result["most_expensive"]["eur_per_mwh"], 120.75)
        self.assertEqual(result["average_eur_per_mwh"], 95.44)
        self.assertEqual((result["currency"], result["resolution_minutes"]), ("EUR", 15))

    def test_document_without_series_is_an_upstream_error(self):
        metadata = dataset("electricity", [("xml", "https://download.data.public.lu/day.xml", "day", "2026-09-17")])
        empty = b'<?xml version="1.0"?><Publication_MarketDocument xmlns="urn:x"/>'
        with self.assertRaises(UpstreamError):
            LuxembourgData(ScriptedHttp([metadata, empty])).get_electricity_prices()


class SharedMobilityTests(unittest.TestCase):
    flex_csv = ("car_type;fuel_type;available;brand_name;model_name;station_name;station_town;station_zipcode;station_street;station_streetnumber;station_latitude;station_longitude\n"
                "E-Car;electric;true;VW;ID.3;Mamer/Holzem - Centre;Mamer;8277;route de Garnich;1;49.62;6.02\n"
                "Transporter;diesel;true;VW;Crafter;Gare;Ettelbruck;9051;avenue JF Kennedy;2;49.84;6.10\n").encode("utf-8")
    velok_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<velok><station><nstation>1</nstation><nom>Avenue de la Gare</nom><lieu>Rue de l'Alzette</lieu>
  <latitude>49.494730</latitude><longitude>5.982760</longitude><nomlocalite>Esch-sur-Alzette</nomlocalite>
  <nomcommune>Esch-sur-Alzette</nomcommune><maintenance>0</maintenance><bikes>3</bikes><ebikes>2</ebikes><libres>7</libres></station>
<station><nstation>2</nstation><nom>Belval</nom><lieu>Avenue du Rock</lieu>
  <latitude>49.50</latitude><longitude>5.94</longitude><nomlocalite>Belvaux</nomlocalite>
  <nomcommune>Sanem</nomcommune><maintenance>1</maintenance><bikes>0</bikes><ebikes>0</ebikes><libres>12</libres></station></velok>"""

    def test_carsharing_filters_by_town_and_fuel(self):
        metadata = dataset("flex", [("csv", "https://download.data.public.lu/flex.csv", "stations", "2026-09-17")])
        result = LuxembourgData(ScriptedHttp([metadata, self.flex_csv])).get_carsharing(query="Mamer", fuel_type="electric")
        self.assertEqual(result["count"], 1)
        vehicle = result["vehicles"][0]
        self.assertEqual((vehicle["town"], vehicle["vehicle"]), ("Mamer", "VW ID.3"))
        self.assertEqual(vehicle["address"], "route de Garnich 1")
        self.assertEqual(vehicle["latitude"], 49.62)

    def test_bike_sharing_counts_and_available_only(self):
        data = LuxembourgData(ScriptedHttp([self.velok_xml]))
        result = data.get_bike_sharing()
        self.assertEqual((result["count"], result["total_bikes"]), (2, 5))
        self.assertTrue(result["stations"][0]["in_maintenance"] is False or result["stations"][1]["in_maintenance"])
        available = LuxembourgData(ScriptedHttp([self.velok_xml])).get_bike_sharing(available_only=True)
        self.assertEqual(available["count"], 1)
        self.assertEqual(available["stations"][0]["locality"], "Esch-sur-Alzette")

    def test_empty_bike_feed_is_an_upstream_error(self):
        with self.assertRaises(UpstreamError):
            LuxembourgData(ScriptedHttp([b"<velok></velok>"])).get_bike_sharing()


class TenderTests(unittest.TestCase):
    payload = {
        "hydra:totalItems": 773,
        "hydra:member": [{
            "id": 532194, "reference": "2402754", "intitule": "Access to databases",
            "organismeDenomination": "Portail des marchés publics", "directionServiceLibelle": "Administration des Marchés Publics",
            "typeProcedureLibelle": "13 européenne", "naturePrestationLibelle": "Fournitures",
            "dateMiseEnLigneCalcule": "2024-12-11T11:33:01+01:00", "dateLimiteRemiseOffres": "2026-12-09T16:30:00+01:00",
            "codeCpvPrincipal": "48611000", "valeurEstimee": 0, "urlConsultation": "https://pmp.b2g.etat.lu/entreprise/consultation/532194",
        }],
    }

    def test_requests_json_ld_and_shapes_notices(self):
        http = ScriptedHttp([self.payload])
        result = LuxembourgData(http).search_tenders(query="database", limit=5)
        url, headers = http.calls[0]
        self.assertEqual(headers, {"Accept": "application/ld+json"})
        self.assertIn("statutCalcule=2", url)
        self.assertIn("itemsPerPage=5", url)
        self.assertIn("search_full", url)
        notice = result["tenders"][0]
        self.assertEqual((result["total"], result["count"]), (773, 1))
        self.assertEqual(notice["buyer"], "Administration des Marchés Publics")
        self.assertEqual(notice["cpv_code"], "48611000")
        self.assertIsNone(notice["estimated_value_eur"])

    def test_open_only_can_be_disabled(self):
        http = ScriptedHttp([self.payload])
        LuxembourgData(http).search_tenders(open_only=False)
        self.assertNotIn("statutCalcule", http.calls[0][0])


if __name__ == "__main__":
    unittest.main()
