import re

PASSWORD_MIN_LENGTH = 8


def validate_password(password):
    """비밀번호가 정책을 만족하는지 검사해 위반 사항 메시지 목록을 반환한다. 빈 리스트면 통과."""
    errors = []
    if len(password) < PASSWORD_MIN_LENGTH:
        errors.append(f'최소 {PASSWORD_MIN_LENGTH}자 이상')
    if not re.search(r'[A-Z]', password):
        errors.append('영문 대문자 1개 이상')
    if not re.search(r'[a-z]', password):
        errors.append('영문 소문자 1개 이상')
    if not re.search(r'[0-9]', password):
        errors.append('숫자 1개 이상')
    if not re.search(r'[^A-Za-z0-9]', password):
        errors.append('특수문자 1개 이상')
    return errors
