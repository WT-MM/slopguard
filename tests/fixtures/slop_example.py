"""Fixture: deliberately slop-ridden Python for slopguard's tests."""
import integrations.payment_gateway
import json
import os  # unused-import: never referenced


# fetch the user record
def fetch_user_record(records, user_id, cache={}):  # mutable-default
    for record in records:
        if record.get("id") == user_id:
            cache[user_id] = record
            return record
    return None


def get_user_record(items, target_id, memo={}):  # duplicate-function of the above
    for entry in items:
        if entry.get("id") == target_id:
            memo[target_id] = entry
            return entry
    return None


def process_payment(amount):
    # In a real implementation, this would call the payment gateway.
    pass  # placeholder-body + hedging-comment


def risky():
    try:
        value = json.loads("{}")
        return value
        print("done")  # dead-code: after return
    except:  # bare-except
        pass  # swallowed-exception


class SessionManager:
    def __init__(self):
        self._retry_count = 0  # write-only-attr: never read
        self.name = "session"

    def _cleanup_stale(self):  # unused-private: never called
        self.name = ""

    def touch(self):
        return self.name


class Greeter:  # single-method-class
    def greet(self, who):
        return "hello " + who
