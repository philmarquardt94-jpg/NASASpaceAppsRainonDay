#Run this in a regular conda python 3.11.8 venv with default packages and it should work
#To do this run this in terminal (make sure anaconda isintalled) https://www.anaconda.com/download/success
#conda create -p ./AppEnv python=3.11.8
#conda activate ./AppEnv
python app.py

#After activating may need to close and reopen terminal
#After running app.py click on this link http://127.0.0.1:5000/
#This link should take you to website to where you can do trip planning, and click on map.


#"open-meteo" — current behavior (forecasts).

#"nasa-power" — historical/near-real-time only. Works for past windows.

#"combined" — Open-Meteo for the values + POWER for uncertainty (when available for more closer data ~1.5 days, afterwards is unreliable).

#If your window is in the future, combined mode still evaluates with Open-Meteo, and tries to fetch POWER for today or earlier and uses that to estimate the flip probability.

#If POWER isn’t available for the range, it gracefully falls back to the Open-Meteo “extended-window” heuristic for uncertainty (so you always get the yellow/green/red %).

Give fact it is none for chance to flip on rain.
