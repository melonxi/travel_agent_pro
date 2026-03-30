# backend/tests/test_normalizers.py
from __future__ import annotations

import pytest


def test_normalize_amadeus_flight():
    from tools.normalizers import normalize_amadeus_flight, FlightResult

    raw = {
        "id": "1",
        "price": {"total": "3500.00", "currency": "CNY"},
        "itineraries": [
            {
                "duration": "PT3H30M",
                "segments": [
                    {
                        "carrierCode": "CA",
                        "number": "1234",
                        "departure": {"iataCode": "PEK", "at": "2026-07-15T08:00:00"},
                        "arrival": {"iataCode": "NRT", "at": "2026-07-15T12:30:00"},
                    }
                ],
            }
        ],
    }
    result = normalize_amadeus_flight(raw)
    assert isinstance(result, FlightResult)
    assert result.airline == "CA"
    assert result.flight_no == "CA1234"
    assert result.price == 3500.0
    assert result.source == "amadeus"
    assert result.booking_url is None


def test_normalize_flyai_flight():
    from tools.normalizers import normalize_flyai_flight, FlightResult

    raw = {
        "title": "东方航空 MU5101",
        "journeys": [
            {
                "segments": [
                    {
                        "airlineName": "东方航空",
                        "flightNo": "MU5101",
                        "depCityName": "上海",
                        "arrCityName": "北京",
                        "depTime": "2026-07-15 08:00",
                        "arrTime": "2026-07-15 10:30",
                        "duration": 150,
                        "stopCount": 0,
                        "cabin": "经济舱",
                    }
                ]
            }
        ],
        "price": "800",
        "jumpUrl": "https://www.fliggy.com/flight/123",
    }
    result = normalize_flyai_flight(raw)
    assert isinstance(result, FlightResult)
    assert result.flight_no == "MU5101"
    assert result.price == 800.0
    assert result.source == "flyai"
    assert result.booking_url == "https://www.fliggy.com/flight/123"


def test_merge_flights_dedup():
    from tools.normalizers import FlightResult, merge_flights

    amadeus = FlightResult(
        airline="MU",
        flight_no="MU5101",
        origin="上海",
        destination="北京",
        dep_time="2026-07-15T08:00:00",
        arr_time="2026-07-15T10:30:00",
        duration_min=150,
        stops=0,
        price=900.0,
        currency="CNY",
        cabin_class="economy",
        source="amadeus",
        booking_url=None,
    )
    flyai = FlightResult(
        airline="东方航空",
        flight_no="MU5101",
        origin="上海",
        destination="北京",
        dep_time="2026-07-15 08:00",
        arr_time="2026-07-15 10:30",
        duration_min=150,
        stops=0,
        price=800.0,
        currency="CNY",
        cabin_class="经济舱",
        source="flyai",
        booking_url="https://www.fliggy.com/flight/123",
    )
    merged = merge_flights([amadeus], [flyai])
    # Same flight_no + same dep date → deduplicated to one, prefer with booking_url
    assert len(merged) == 1
    assert merged[0].booking_url is not None
    assert merged[0].source == "flyai"


def test_normalize_google_accommodation():
    from tools.normalizers import normalize_google_accommodation, AccommodationResult

    raw = {
        "name": "Park Hyatt Tokyo",
        "formatted_address": "3-7-1-2 Nishi Shinjuku",
        "rating": 4.6,
        "geometry": {"location": {"lat": 35.6894, "lng": 139.6917}},
        "price_level": 4,
    }
    result = normalize_google_accommodation(raw)
    assert isinstance(result, AccommodationResult)
    assert result.name == "Park Hyatt Tokyo"
    assert result.lat == 35.6894
    assert result.source == "google"


def test_flyai_hotel_null_price():
    from tools.normalizers import normalize_flyai_hotel, AccommodationResult

    raw = {
        "title": "全季酒店",
        "address": "杭州市西湖区",
        "score": "4.5",
        "detailUrl": "https://www.fliggy.com/hotel/456",
        "price": None,
    }
    result = normalize_flyai_hotel(raw)
    assert isinstance(result, AccommodationResult)
    assert result.price_per_night is None
    assert result.booking_url == "https://www.fliggy.com/hotel/456"


def test_merge_pois_fuzzy():
    from tools.normalizers import POIResult, merge_pois

    google = POIResult(
        name="Kinkaku-ji Temple",
        address="1 Kinkakujicho, Kyoto",
        lat=35.0394,
        lng=135.7292,
        rating=4.7,
        category=None,
        is_free=None,
        ticket_price=None,
        ticket_url=None,
        source="google",
        booking_url=None,
    )
    flyai = POIResult(
        name="金阁寺 Kinkaku-ji",
        address="京都市北区",
        lat=None,
        lng=None,
        rating=4.8,
        category="名胜古迹",
        is_free=False,
        ticket_price=400.0,
        ticket_url="https://fliggy.com/poi/789",
        source="flyai",
        booking_url="https://fliggy.com/poi/789",
    )
    merged = merge_pois([google], [flyai])
    # Fuzzy match > 0.8 → merged record with Google coords + FlyAI ticket info
    matched = [p for p in merged if p.source == "merged"]
    assert len(matched) == 1
    assert matched[0].lat == 35.0394  # from Google
    assert matched[0].ticket_price == 400.0  # from FlyAI
    assert matched[0].category == "名胜古迹"  # from FlyAI
