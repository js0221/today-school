"""공개 학생용 컴시간 시간표를 읽는 어댑터. 외부 JavaScript는 실행하지 않습니다.

공개 화면 http://comci.net:4082/st 의 요청 형식과 자료 구조를 확인하여 구현.
공식 제휴 API가 아니므로 공개 화면의 구조 변경 시 어댑터 수정이 필요합니다.
계정·비밀번호·학생 이름은 컴시간 서버로 보내지 않습니다.
"""
import base64
import copy
import json
import re
import threading
import time
from datetime import date, timedelta
from urllib.parse import quote_from_bytes, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'http://comci.net:4082'
_cache = {}
_lock = threading.RLock()


class ComciganError(Exception):
    pass


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).netloc != urlsplit(BASE).netloc:
            raise ComciganError('컴시간 주소가 변경되었습니다. 관리자에게 알려 주세요.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def cached(key, ttl, fetch):
    # 공개 서버 부하를 줄이기 위해 검색·시간표는 짧게 캐시한다.
    with _lock:
        entry = _cache.get(key)
        if entry and entry[0] > time.monotonic():
            return copy.deepcopy(entry[1])
        value = fetch()
        if len(_cache) > 128:
            _cache.clear()
        _cache[key] = (time.monotonic() + ttl, value)
        return copy.deepcopy(value)


def read(path):
    if not path.startswith('/'):
        raise ComciganError('컴시간 요청 주소가 올바르지 않습니다.')
    request = Request(BASE + path, headers={
        'User-Agent': 'Mozilla/5.0 (School Companion; student timetable)',
        'Referer': BASE + '/st',
        'Accept': '*/*',
    })
    try:
        with build_opener(SameOriginRedirect()).open(request, timeout=7) as response:
            raw = response.read(5_000_001)
        if len(raw) > 5_000_000:
            raise ComciganError('컴시간 응답 크기를 확인할 수 없습니다.')
        for encoding in ('utf-8-sig', 'cp949'):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                pass
    except (OSError, ValueError) as error:
        raise ComciganError('컴시간 서버에 연결하지 못했습니다. 잠시 후 다시 불러오세요.') from error
    raise ComciganError('컴시간 응답의 문자 형식이 변경되었습니다.')


def protocol():
    def fetch():
        source = read('/st')
        search = re.search(r"url\s*:\s*['\"]\./(\d+)\?(\d+)l", source)
        timetable = re.search(r"sc_data\s*\(\s*['\"](\d+)_", source)
        if not search or not timetable:
            raise ComciganError('컴시간 공개 화면의 형식이 변경되었습니다. 연동 코드를 확인해야 합니다.')
        return search.group(1), search.group(2), timetable.group(1)
    return cached(('protocol',), 3600, fetch)


def response_json(path):
    raw = read(path)
    try:
        value, _ = json.JSONDecoder().raw_decode(raw.lstrip())
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    raise ComciganError('컴시간 응답을 해석하지 못했습니다. 저장된 시간표는 유지됩니다.')


def search_schools(name):
    name = name.strip()
    if not 2 <= len(name) <= 80:
        raise ComciganError('학교 이름을 두 글자 이상 입력하세요.')
    def fetch():
        endpoint, prefix, _ = protocol()
        try:
            encoded = quote_from_bytes(name.encode('cp949'), safe='')
        except UnicodeEncodeError as error:
            raise ComciganError('학교 이름은 한글 또는 영문으로 입력하세요.') from error
        data = response_json('/' + endpoint + '?' + prefix + 'l' + encoded)
        rows = data.get('학교검색')
        if not isinstance(rows, list):
            raise ComciganError('학교 검색 응답 형식이 변경되었습니다.')
        schools = []
        for row in rows:
            if isinstance(row, list) and len(row) >= 4 and str(row[3]).isdigit() and int(row[3]) > 0:
                schools.append({'code': int(row[3]), 'region': str(row[1]), 'name': str(row[2])})
        return schools
    return cached(('schools', name), 300, fetch)


def get_week(school_code, week):
    if type(school_code) is not int or not 1 <= school_code <= 99999999 or week not in (1, 2):
        raise ComciganError('학교와 조회 주를 확인하세요.')
    def fetch():
        endpoint, _, prefix = protocol()
        payload = base64.b64encode(f'{prefix}_{school_code}_0_{week}'.encode('ascii')).decode('ascii')
        data = response_json('/' + endpoint + '?' + payload)
        if not data or not isinstance(data.get('자료147'), (list, dict)):
            raise ComciganError('해당 학교의 공개 시간표가 없습니다.')
        return data
    return cached(('week', school_code, week), 60, fetch)


def item(container, index, default=None):
    if isinstance(container, list) and 0 <= index < len(container):
        return container[index]
    if isinstance(container, dict):
        return container.get(str(index), default)
    return default


def normalize(data, grade, classroom):
    """현재 공개 화면처럼 자료147(변경 반영)을 사용. 취소 수업을 원본으로 복구하지 않음."""
    try:
        start = date.fromisoformat(data['시작일'])
        classes = int(item(data.get('학급수'), grade, 0))
        virtual = int(item(data.get('가상학급수'), grade, 0))
        divisor = int(data.get('분리', 100))
    except (ValueError, TypeError, KeyError) as error:
        raise ComciganError('컴시간 시간표의 날짜·학급 형식을 확인할 수 없습니다.') from error
    if divisor not in (100, 1000) or start.weekday() != 0:
        raise ComciganError('지원하지 않는 시간표 형식입니다. 컴시간 원본에서 확인해 주세요.')
    if not 1 <= classroom <= classes - virtual:
        raise ComciganError('해당 학년·반이 없습니다. 학년과 반을 확인하세요.')
    matrix = item(item(data['자료147'], grade), classroom)
    if not isinstance(matrix, (list, dict)):
        raise ComciganError('해당 학급의 공개 시간표가 없습니다.')
    try:
        cutoff = date.fromisoformat(str(data.get('열람제한일', ''))[:10])
    except ValueError:
        cutoff = None
    timetable, count, restricted = {}, 0, []
    for weekday in range(5):
        target = start + timedelta(days=weekday)
        if cutoff and cutoff.year >= 2014 and target >= cutoff:
            timetable[str(weekday)] = []
            restricted.append(weekday)
            continue
        row = item(matrix, weekday + 1, [])
        try:
            periods = int(item(row, 0, 0))
        except (ValueError, TypeError) as error:
            raise ComciganError('시간표 교시 형식을 해석하지 못했습니다.') from error
        if not 0 <= periods <= 12:
            raise ComciganError('지원하지 않는 수업 수입니다.')
        lessons = []
        for period in range(1, periods + 1):
            raw = str(item(row, period, 0))
            numeric = raw[1:] if raw.startswith('>') else raw
            if not numeric.isdigit():
                raise ComciganError('수업 코드 형식이 변경되었습니다.')
            code = int(numeric)
            teacher = code % 1000
            subject_code = (code // 1000) % divisor
            subject = item(data.get('자료492'), subject_code, '') if teacher else ''
            if subject and not isinstance(subject, str):
                raise ComciganError('과목 이름 형식을 확인할 수 없습니다.')
            if teacher and code and not subject:
                raise ComciganError('과목 코드를 찾을 수 없습니다. 저장된 시간표는 유지됩니다.')
            group = (code // 1000) // divisor
            prefix = chr(64 + group) + '_' if divisor == 1000 and 1 <= group <= 26 else ''
            lessons.append({'subject': prefix + subject if subject else '공강', 'supplies': ''})
            count += bool(subject)
        timetable[str(weekday)] = lessons
    if not count:
        raise ComciganError('공개된 수업이 없습니다. 열람 제한 또는 미등록된 주인지 컴시간에서 확인하세요.')
    return {'days': timetable, 'week_start': start.isoformat(),
            'week_end': (start + timedelta(days=4)).isoformat(),
            'upstream_updated': str(data.get('자료244', ''))[:60], 'restricted_days': restricted}


def preview(school_name, school_code, grade, classroom, week):
    schools = search_schools(school_name)
    school = next((school for school in schools if school['code'] == school_code), None)
    if not school:
        raise ComciganError('검색 결과에서 학교를 다시 선택하세요.')
    result = normalize(get_week(school_code, week), grade, classroom)
    result.update(school=school, grade=grade, classroom=classroom, source='컴시간알리미')
    return result
