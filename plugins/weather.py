import requests
from sqlalchemy import Table, Column, PrimaryKeyConstraint, String

from cloudbot import hook
from cloudbot.util import web, database


# Define a database table to store the last-searched location
table = Table(
    "weather",
    database.metadata,
    Column('nick', String),
    Column('loc', String),
    PrimaryKeyConstraint('nick')
)

location_cache = {}

# Define some constants
google_base = 'https://maps.googleapis.com/maps/api/'
geocode_api = google_base + 'geocode/json'
wunder_api = "http://api.wunderground.com/api/{}/forecast/lang:{}/geolookup/conditions/q/{}.json"

# Change this to a ccTLD code (eg. uk, nz) to make results more targeted towards that specific country.
# <https://developers.google.com/maps/documentation/geocoding/#RegionCodes>
bias = None


class GeocodeAPIError(Exception):
    """Raised when the geocode api returns an error message.
    This helps error messages optionally be returned *en francais*."""
    def __init__(self, status):
        super()
        self._status = status

    def __str__(self):
        if self._status == 'REQUEST_DENIED':
            return 'The geocode API is off in the Google Developers Console.'
        elif self._status == 'ZERO_RESULTS':
            return 'No results found.'
        elif self._status == 'OVER_QUERY_LIMIT':
            return 'The geocode API quota has run out.'
        elif self._status == 'UNKNOWN_ERROR':
            return 'Unknown Error.'
        elif self._status == 'INVALID_REQUEST':
            return 'Invalid Request.'
        else:
            return repr(self._status)

    def en_francais(self):
        if self._status == 'REQUEST_DENIED':
            return "L'API de géocodage est désactivée dans la console des développeurs Google."
        elif self._status == 'ZERO_RESULTS':
            return "Aucun resultat n'a été trouvé."
        elif self._status == 'OVER_QUERY_LIMIT':
            return "Le quota de API de géocodage est épuisé."
        elif self._status == 'UNKNOWN_ERROR':
            return 'Quelque chose a mal tourné.'
        elif self._status == 'INVALID_REQUEST':
            return 'Il y a eu une demande invalide.'
        else:
            return 'La France a été trahie! {!r}'.format(self._status)


def find_location(location):
    """
    Takes a location as a string, and returns a dict of data
    :param location: string
    :return: dict
    """
    params = {"address": location, "key": dev_key}
    if bias:
        params['region'] = bias

    request = requests.get(geocode_api, params=params)
    request.raise_for_status()

    json = request.json()

    if json['status'] != 'OK':
        raise GeocodeAPIError(json['status'])

    return json['results'][0]['geometry']['location']


def load_cache(db):
    """
    :type db: sqlalchemy.orm.Session
    """
    for row in db.execute(table.select()):
        nick = row["nick"]
        location = row["loc"]
        location_cache[nick] = location


def set_location(nick, location, db):
    """
    :type nick: str
    :type location: str
    :type db: sqlalchemy.orm.Session
    """
    nick, location = nick.lower(), location.lower()
    if nick in location_cache:
        statement = table.update().values(loc=location).where(table.c.nick == nick)
    else:
        statement = table.insert().values(nick=nick, loc=location)
    db.execute(statement)
    db.commit()
    load_cache(db)


def get_location(nick):
    """looks in location_cache for a saved location"""
    return location_cache.get(nick.lower(), None)


@hook.on_start
def on_start(bot, db):
    """ Loads API keys
    :type bot: cloudbot.bot.Cloudbot
    :type db: sqlalchemy.orm.Session
    """
    global dev_key, wunder_key
    dev_key = bot.config.get("api_keys", {}).get("google_dev_key", None)
    wunder_key = bot.config.get("api_keys", {}).get("wunderground", None)
    load_cache(db)


class APIKeyMissing(Exception):
    """Raised when an API key is missing.
    This helps error messages optionally be returned *en francais*."""
    def __init__(self, name):
        super()
        self._name = name

    def __str__(self):
        return 'This command requires a {} API key.'.format(self._name)

    def en_francais(self):
        return 'Cette commande nécessite une clé API {}.'.format(self._name)


def get_weather_data(text, db, nick, notice_doc, language='EN'):
    """Get weather data from Weather Underground.
    :type text: str
    :type db: sqlalchemy.orm.Session
    :type nick: str
    :type notice_doc: Callable
    :param str language: two-letter language code
    (see https://www.wunderground.com/weather/api/d/docs?d=language-support)
    """
    if not wunder_key:
        raise APIKeyMissing('Weather Underground')
    if not dev_key:
        raise APIKeyMissing('Google Developers Console')

    # If no input try the db
    if not text:
        location = get_location(nick)
        if not location:
            notice_doc()
            return
    else:
        location = text

    # use find_location to get location data from the user input
    location_data = find_location(location)

    formatted_location = "{lat},{lng}".format(**location_data)

    url = wunder_api.format(wunder_key, language, formatted_location)
    request = requests.get(url)
    request.raise_for_status()

    response = request.json()

    error = response['response'].get('error')
    if error:
        return "{}".format(error['description'])

    forecast = response["forecast"]["simpleforecast"]["forecastday"]
    if not forecast:
        return 'Unable to retrieve forecast data.'

    forecast_today = forecast[0]
    forecast_tomorrow = forecast[1]

    forecast_today_high = forecast_today['high']
    forecast_today_low = forecast_today['low']
    forecast_tomorrow_high = forecast_tomorrow['high']
    forecast_tomorrow_low = forecast_tomorrow['low']

    current_observation = response['current_observation']

    # put all the stuff we want to use in a dictionary for easy formatting of the output
    weather_data = {
        "place": current_observation['display_location']['full'],
        "conditions": current_observation['weather'],
        "temp_f": current_observation['temp_f'],
        "temp_c": current_observation['temp_c'],
        "humidity": current_observation['relative_humidity'],
        "wind_kph": current_observation['wind_kph'],
        "wind_mph": current_observation['wind_mph'],
        "wind_direction": current_observation['wind_dir'],
        "today_conditions": forecast_today['conditions'],
        "today_high_f": forecast_today_high['fahrenheit'],
        "today_high_c": forecast_today_high['celsius'],
        "today_low_f": forecast_today_low['fahrenheit'],
        "today_low_c": forecast_today_low['celsius'],
        "tomorrow_conditions": forecast_tomorrow['conditions'],
        "tomorrow_high_f": forecast_tomorrow_high['fahrenheit'],
        "tomorrow_high_c": forecast_tomorrow_high['celsius'],
        "tomorrow_low_f": forecast_tomorrow_low['fahrenheit'],
        "tomorrow_low_c": forecast_tomorrow_low['celsius'],
    }

    # Get the more accurate URL if available, if not, get the generic one.
    ob_url = current_observation['ob_url']
    if "?query=," in ob_url:
        url = current_observation['forecast_url']
    else:
        url = ob_url

    weather_data['url'] = web.try_shorten(url)

    if text:
        set_location(nick, location, db)

    return weather_data


@hook.command("weather", "we", autohelp=False)
def weather(text, reply, db, nick, notice_doc):
    """<location> - Gets weather data for <location>.
    :type text: str
    :type reply: Callable
    :type db: sqlalchemy.orm.Session
    :type nick: str
    :type notice_doc: Callable
    """
    try:
        weather_data = get_weather_data(text, db, nick, notice_doc)
    except (APIKeyMissing, GeocodeAPIError) as e:
        return str(e)
    if not isinstance(weather_data, dict):
        return weather_data
    reply("{place} - \x02Current:\x02 {conditions}, "
          "{temp_f}F/{temp_c}C, {humidity}, "
          "Wind: {wind_mph}MPH/{wind_kph}KPH {wind_direction}, "
          "\x02Today:\x02 {today_conditions}, "
          "High: {today_high_f}F/{today_high_c}C, "
          "Low: {today_low_f}F/{today_low_c}C. "
          "\x02Tomorrow:\x02 {tomorrow_conditions}, "
          "High: {tomorrow_high_f}F/{tomorrow_high_c}C, "
          "Low: {tomorrow_low_f}F/{tomorrow_low_c}C - {url}".format_map(weather_data))


# 'oui' is a pun, see https://github.com/snoonetIRC/CloudBot/issues/271
@hook.command('météo', 'meteo', 'oui', autohelp=False)
def meteo(text, reply, db, nick, notice_doc):
    """<lieu> - Quel temps fait-il à <lieu>?
    :type text: str
    :type reply: Callable
    :type db: sqlalchemy.orm.Session
    :type nick: str
    :type notice_doc: Callable
    """
    try:
        weather_data = get_weather_data(text, db, nick, notice_doc, language='FR')
    except (APIKeyMissing, GeocodeAPIError) as e:
        return e.en_francais()
    if not isinstance(weather_data, dict):
        if weather_data == 'Unable to retrieve forecast data.':
            weather_data = 'Impossible de récupérer les données météorologiques.'
        return weather_data
    reply("{place} - \x02Actuelle:\x02 {conditions}, "
          "{temp_f}F/{temp_c}C, {humidity}, "
          "Vent: {wind_mph}MPH/{wind_kph}KPH {wind_direction}, "
          "\x02Aujourd'hui:\x02 {today_conditions}, "
          "Haute: {today_high_f}F/{today_high_c}C, "
          "Basse: {today_low_f}F/{today_low_c}C. "
          "\x02Demain:\x02 {tomorrow_conditions}, "
          "Haute: {tomorrow_high_f}F/{tomorrow_high_c}C, "
          "Basse: {tomorrow_low_f}F/{tomorrow_low_c}C - {url}".format_map(weather_data))
