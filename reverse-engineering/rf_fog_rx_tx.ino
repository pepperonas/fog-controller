#include <RCSwitch.h>

RCSwitch mySwitch = RCSwitch();
const unsigned long CODE_ON = 4543756;  // Code für "Ein"
const unsigned long CODE_OFF = 4543792; // Code für "Aus"
const int TRANSMIT_PIN = 10;            // Pin für Sender (FS1000A)
const int RECEIVE_PIN = 0;              // Interrupt 0 (Pin 2 auf Arduino Uno)

void setup() {
  Serial.begin(9600);
  
  // Sender konfigurieren
  mySwitch.enableTransmit(TRANSMIT_PIN);
  mySwitch.setPulseLength(320);  // Standard-Pulselength (anpassen falls nötig)
  mySwitch.setProtocol(1);       // Protokoll 1 (Standard)
  
  // Empfänger aktivieren
  mySwitch.enableReceive(RECEIVE_PIN);
  
  Serial.println("433MHz Sender/Empfänger bereit!");
  Serial.println("Befehle: '1' = EIN, '0' = AUS");
  Serial.println("Empfange auch Signale von anderen Geräten...\n");
}

void loop() {
  // SENDEN: Serielle Eingabe prüfen
  if (Serial.available()) {
    char input = Serial.read();
    
    // Kurz Empfänger deaktivieren beim Senden
    mySwitch.disableReceive();
    
    if (input == '1') {
      mySwitch.send(CODE_ON, 24);
      Serial.println(">>> GESENDET: EIN (4543756)");
    } else if (input == '0') {
      mySwitch.send(CODE_OFF, 24);
      Serial.println(">>> GESENDET: AUS (4543792)");
    }
    
    // Empfänger wieder aktivieren
    delay(100);  // Kurze Pause nach dem Senden
    mySwitch.enableReceive(RECEIVE_PIN);
  }

  // EMPFANGEN: Signale prüfen
  if (mySwitch.available()) {
    unsigned long value = mySwitch.getReceivedValue();
    
    if (value != 0) {
      Serial.print("<<< EMPFANGEN: ");
      Serial.print(value);
      Serial.print(" / ");
      Serial.print(mySwitch.getReceivedBitlength());
      Serial.print(" Bit / Protokoll: ");
      Serial.print(mySwitch.getReceivedProtocol());
      Serial.print(" / Pulselength: ");
      Serial.println(mySwitch.getReceivedDelay());
      
      // Erkenne bekannte Codes
      if (value == CODE_ON) {
        Serial.println("    => Das war unser EIN-Signal!");
      } else if (value == CODE_OFF) {
        Serial.println("    => Das war unser AUS-Signal!");
      }
    }
    
    mySwitch.resetAvailable();
  }
}