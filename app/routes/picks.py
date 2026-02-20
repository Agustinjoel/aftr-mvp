from fastapi import APIRouter, Query
from data.cache import read_json
from models.enums import DEFAULT_LEAGUE, LEAGUES

router = APIRouter()

@router.get("/picks")
def get_picks(league: str = Query(DEFAULT_LEAGUE)):
    league = league if league in LEAGUES else DEFAULT_LEAGUE
    return read_json(f"daily_picks_{league}.json")