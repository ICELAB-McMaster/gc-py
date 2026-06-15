from gcodepy.gcode import Gcode
import json
import os
import sys

with open("examples/parameters.json",'r') as file:
    parameters = json.load(file)

name = "stagetourjson"

for key, values in parameters.items():
    globals()[key] = values

g = Gcode(f"{name}.gcode")                      #Name Gcode File

g.home("Z")                                     #Homing Z
g.home("XY")                                    #Homing XY

for pos,cord in centers.items():
    x,y = cord
    g.travel_absolute((x,y,g.get_z()),feedrate=feedspeed)  #Absoulte Travel
    g.travel((0,0,-20),feedrate=feedspeedhold)  #Relative Travel
    g.wait_finish()                             #Wait for Movement to Finish
    g.dwell(10)                                 #Wait 10 seconds
    g.travel((0,0,20),feedrate=feedspeedhold)

g.close()

print(f"done! check out {name}.gcode")

#Example Worked!!