#!/usr/bin/env python3
"""
RF433 Fog Machine Controller for Raspberry Pi
Uses GPIO to send 433MHz signals to control a fog machine

Hardware setup:
- RF433 transmitter DATA pin: GPIO 17 (Pin 11)
- RF433 VCC: 5V (Pin 2 or 4)
- RF433 GND: GND (Pin 6 or 9)

Protocol details from Arduino code:
- Pulse Length: 320 microseconds
- Protocol: 1 (standard)
- Bit Length: 24 bits
- ON Code: 4543756
- OFF Code: 4543792
"""

import lgpio
import time
import argparse
import sys

class RF433Controller:
    def __init__(self, gpio_pin=17):
        self.gpio_pin = gpio_pin
        self.pulse_length = 320  # microseconds
        self.protocol = 1
        self.repeat_transmit = 10  # Number of times to repeat the signal
        
        # RF codes from Arduino
        self.CODE_ON = 4543756
        self.CODE_OFF = 4543792
        
        # Protocol 1 timing (from RCSwitch)
        self.sync_high = 1
        self.sync_low = 31
        self.zero_high = 1
        self.zero_low = 3
        self.one_high = 3
        self.one_low = 1
        
        # Setup GPIO using lgpio
        self.handle = lgpio.gpiochip_open(0)
        lgpio.gpio_claim_output(self.handle, self.gpio_pin)
        lgpio.gpio_write(self.handle, self.gpio_pin, 0)
        
    def cleanup(self):
        """Explicit cleanup method"""
        try:
            lgpio.gpiochip_close(self.handle)
        except:
            pass  # Ignore cleanup errors
    
    def transmit_bit(self, bit):
        """Transmit a single bit using Protocol 1 timing"""
        if bit:
            # Send '1' bit: HIGH for 3 units, LOW for 1 unit
            lgpio.gpio_write(self.handle, self.gpio_pin, 1)
            time.sleep((self.one_high * self.pulse_length) / 1000000.0)
            lgpio.gpio_write(self.handle, self.gpio_pin, 0)
            time.sleep((self.one_low * self.pulse_length) / 1000000.0)
        else:
            # Send '0' bit: HIGH for 1 unit, LOW for 3 units
            lgpio.gpio_write(self.handle, self.gpio_pin, 1)
            time.sleep((self.zero_high * self.pulse_length) / 1000000.0)
            lgpio.gpio_write(self.handle, self.gpio_pin, 0)
            time.sleep((self.zero_low * self.pulse_length) / 1000000.0)
    
    def transmit_sync(self):
        """Send sync signal: HIGH for 1 unit, LOW for 31 units"""
        lgpio.gpio_write(self.handle, self.gpio_pin, 1)
        time.sleep((self.sync_high * self.pulse_length) / 1000000.0)
        lgpio.gpio_write(self.handle, self.gpio_pin, 0)
        time.sleep((self.sync_low * self.pulse_length) / 1000000.0)
    
    def send_code(self, code, length=24):
        """Send a code multiple times"""
        for _ in range(self.repeat_transmit):
            # Send the bits (MSB first)
            for i in range(length - 1, -1, -1):
                bit = (code >> i) & 1
                self.transmit_bit(bit)
            
            # Send sync signal
            self.transmit_sync()
        
        # Ensure pin is low after transmission
        lgpio.gpio_write(self.handle, self.gpio_pin, 0)
    
    def turn_on(self):
        """Turn the fog machine ON"""
        print(f"Sending ON signal: {self.CODE_ON} (0x{self.CODE_ON:06X})")
        self.send_code(self.CODE_ON)
        print("ON signal sent successfully")
        return True
    
    def turn_off(self):
        """Turn the fog machine OFF"""
        print(f"Sending OFF signal: {self.CODE_OFF} (0x{self.CODE_OFF:06X})")
        self.send_code(self.CODE_OFF)
        print("OFF signal sent successfully")
        return True
    
    def send_custom_code(self, code):
        """Send a custom code (for testing)"""
        print(f"Sending custom code: {code} (0x{code:06X})")
        self.send_code(code)
        print("Custom code sent successfully")
        return True

def main():
    parser = argparse.ArgumentParser(description='RF433 Fog Machine Controller')
    parser.add_argument('--command', choices=['on', 'off', 'custom'], 
                        required=True, help='Command to send')
    parser.add_argument('--code', type=lambda x: int(x, 0), 
                        help='Custom code to send (decimal or hex with 0x prefix)')
    parser.add_argument('--gpio', type=int, default=17, 
                        help='GPIO pin number (default: 17)')
    parser.add_argument('--repeats', type=int, default=10,
                        help='Number of times to repeat the signal (default: 10)')
    
    args = parser.parse_args()
    
    controller = None
    try:
        controller = RF433Controller(gpio_pin=args.gpio)
        controller.repeat_transmit = args.repeats
        
        if args.command == 'on':
            success = controller.turn_on()
        elif args.command == 'off':
            success = controller.turn_off()
        elif args.command == 'custom':
            if args.code is None:
                print("Error: --code required for custom command")
                sys.exit(1)
            success = controller.send_custom_code(args.code)
        
        sys.exit(0 if success else 1)
        
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)
    finally:
        # Ensure GPIO cleanup happens
        if controller:
            controller.cleanup()

if __name__ == "__main__":
    main()