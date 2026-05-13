from gcodepy.gcode import Gcode
import sys

pathvar = "Insert Your Path"

sys.path.insert(0, pathvar)

feedspeed = 6000                                # 100 mm/s
feedspeedhold = 3000                            # 50 mm/s

pos1 = (-84,365,130)
pos2 = (-220,365,130)
pos3 = (-355,365,130)
pos4 = (-84,272,130)
pos5 = (-220,272,130)
pos6 = (-355,272,130)
pos7 = (-84,178.5,130)
pos8 = (-220,178.5,130)
pos9 = (-355,178.5,130)

poslist = [pos1,pos2,pos3,pos4,pos5,pos6,pos7,pos8,pos9]
name = "stagetour"

g = Gcode(f"{name}.gcode")                      #Name Gcode File

g.home("Z")                                     #Homing Z
g.home("XY")                                    #Homing XY

for each in poslist:
    g.travel_absolute(each,feedrate=feedspeed)  #Absoulte Travel
    g.travel((0,0,-20),feedrate=feedspeedhold)  #Relative Travel
    g.wait_finish()                             #Wait for Movement to Finish
    g.dwell(10)                                 #Wait 10 seconds
    g.travel((0,0,20),feedrate=feedspeedhold)

g.close()

print(f"done! check out {name}.gcode")

#Example Worked!!