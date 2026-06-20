"""Example: subscribe to Redis-published events and print payloads.

Run after ensuring `REDIS_URL` is set (or backend/.env has it loaded).
"""
import time
from module2.storage.event_publisher import EventPublisher


def handle_job_passed(payload):
    print("Received event:job.passed_filters ->", payload)


def main():
    pub = EventPublisher()
    pub.subscribe("event:job.passed_filters", handle_job_passed)
    print("Subscribed to event:job.passed_filters — waiting for messages...")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Exiting")


if __name__ == '__main__':
    main()
