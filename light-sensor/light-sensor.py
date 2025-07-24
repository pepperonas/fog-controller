import RPi.GPIO as GPIO
import time

LDR_PIN = 22  # Freier GPIO-Pin

GPIO.setmode(GPIO.BCM)
GPIO.setup(LDR_PIN, GPIO.IN)

while True:
    if GPIO.input(LDR_PIN) == GPIO.LOW:
        print("Box ist AUS (LED dunkel)")
    else:
        print("Box ist AN (LED leuchtet)")
    
    time.sleep(1)

# Cleanup beim Beenden (Ctrl+C)
try:
    pass
except KeyboardInterrupt:
    GPIO.cleanup()
