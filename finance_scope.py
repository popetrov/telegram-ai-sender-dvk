"""Process-local identity for signed Finance package activation."""

OWNER_ACCOUNT_ID = 925600480
_identity = None


def bind(account, manager, grant, route):
    global _identity
    if (grant.get('allowed') is not True
            or grant.get('subject') != f'{manager}:{account}'
            or grant.get('company_id') != 'dvk'
            or route.get('company_id') != 'dvk'
            or route.get('business_id') != 'dvk_finance'):
        raise PermissionError('Нет подписанного назначения ДВК Финанс для этого аккаунта.')
    identity = (account, manager)
    if _identity is not None and _identity != identity:
        raise PermissionError('Аккаунт или номер менеджера изменился во время работы.')
    _identity = identity


def current_account():
    return _identity[0] if _identity else OWNER_ACCOUNT_ID


def current_manager():
    return _identity[1] if _identity else 'manager_001'


def account_allowed(account):
    return type(account) is int and account == current_account()


def thread_allowed(thread):
    return (account_allowed(thread['account_id'])
            and thread['manager_id'] == current_manager()
            and thread['company_id'] == 'dvk'
            and thread['business_id'] == 'dvk_finance')


def require_access():
    import access_runtime
    grant = access_runtime.require_access()
    if _identity is not None:
        from business_updates import verified_routes
        account, manager = _identity
        if (grant.get('subject') != f'{manager}:{account}'
                or not any(r['company_id'] == 'dvk' and r['business_id'] == 'dvk_finance'
                           for r in verified_routes(access_runtime._store))):
            from access_lease import AccessDenied
            raise AccessDenied('Центр отключил назначение автодиалогов Финанс.')
    return grant
