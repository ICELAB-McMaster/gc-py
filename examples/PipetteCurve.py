from gcodepy.gcode import Gcode
import json
import os
import sys

def getWater(G,x,y,z,feedspeed):
    G.travel_absolute((x,y,z),feedrate=feedspeed)
    G.wait_finish()
    G.file.write(f"ACTUATOR_MOVE POS={GAP_POS} \n")
    G.dwell(1)
    G.travel((0,0,45),feedrate=feedspeed)  #Relative Travel
    G.wait_finish()
    G.file.write(f"ACTUATOR_MOVE POS={GAP_POS-ASPIRATE_POS}\n")
    G.dwell(3)                                 #Wait 3 seconds
    G.travel((0,0,-45),feedrate=feedspeed)

with open("examples/parameters.json",'r') as file:
    parameters = json.load(file)

name = "200ulPipetteCurveFull"
GAP_POS = 26
ASPIRATE_POS   = 5
DISPENSE_POS   = 31
Xlist = list(range(78,187,12))
Ylist = list(range(223,150,-18))
Y = 223

for key, values in parameters.items():
    globals()[key] = values

g = Gcode(f"{name}.gcode")                      #Name Gcode File

g.home("Z")                                     #Homing Z
g.home("XY")                                    #Homing XY
g.file.write(f"HOME_ACTUATOR \n")

g.travel((0,0,5),feedrate=feedspeedhold)  #Relative Travel

for xcord in Xlist:
    getWater(g,0,175,g.get_z(),feedspeed=feedspeed)
    g.travel_absolute((xcord,Y,g.get_z()),feedrate=feedspeed)
    g.travel((0,0,15),feedrate=feedspeedhold)  #Relative Travel
    g.wait_finish()                             #Wait for Movement to Finish
    g.file.write(f"ACTUATOR_MOVE POS={DISPENSE_POS} \n")
    g.dwell(3)                                 #Wait 10 seconds
    g.travel((0,0,-15),feedrate=feedspeedhold)

g.travel_absolute((350,0,0),feedrate=feedspeed)
g.close()

print(f"done! check out {name}.gcode")