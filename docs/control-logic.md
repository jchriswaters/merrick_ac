1. The main thermostat controls heating and cooling on the unico unit.  The theater room and downstairs room thermostats will only modulate the position of the the dampers to each room.
2. When the main thermostat calls for heating and the temperature outside is above a configurable threshold (defaulting to 40 degrees F), then the controller should turn on the bypass valve and call for cooling low on the UNICO unit
3. If the inside temperature does not increase at a rate above a minimmum threshold (default to 1 degree F per 15 minutes) then the controller should call for cooling high to the unico unit.
4. If the outside temerature is below 40 deg F (configurable), then in addition to the logic above to control the unico unit, the controller should also call for heat so that the auxilliary electric heater will turn on.
5. When the main thermostat calls for cooling and the humidity input is not on, then the controller should make a cooling low call to the unico unit
6. if the main thermostat calls for cooling and the humidity input is on then the controller should make a call for cooling high to the unico unit.
7. if there is no call for cooling, but the humidity sensor is on, then the output to the dehumidifier should turn on and the unico fan should turn on.
8. If there is still no call for cooling, and the dehumidifier has been on for at least 20 minutes (configurable) and the humidity sensor is still on, then we need to turn off the dehumidifier and turn on the high cooling to the unico unit until the humidity sensor is off.
9. whenever the dehumidifier is on, the unico should not be cooling, but the fan on the unico unit should be on.
   whenever the unico unit is cooling, the dehumidifier should be off(we don't want the warm air from the dehumidifier heating up the coils on the unico's air condenser) - the dehumidifier's air outlet is into the unico air handler before the condenser coil.
   If the downstairs or theater room thermostats call for cooling, and the unico unit is cooling, or when the downstairs damper calls for heat and the unit is heating, then the damper to that room should be open
   if the humidity outside is > 80% the vent output should always be off.
   If we can get some free cooling when the outside temperature is lower than 60 degrees F (configurable) and the outside humidity is lower than that configurable 80% (same constant as for when the vent should stay closed) then the vent should be open
   Generally we want some continuous air movement through the house, so when no thermostat is calling for heat or for cool, the fan on the unico unit should run and the vents to each room should be open.
   Generally we want to follow fresh air guidance on the house - so would like (as long as the obove constraints are followed) to let in as much fresh air as possible.The house has about 4200 square feet of space - the ceilings are all 10 feet tall.
   
