from fastapi import APIRouter, Query
from data.cache import read_json
from models.enums import DEFAULT_LEAGUE, LEAGUES

router = APIRouter()

@router.get("/matches")
def get_matches(league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    return read_json(f"daily_matches_{league}.json")