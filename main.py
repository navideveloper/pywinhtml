import pywinhtml as win

app = win.App("example.html", "#FFFFFF", width=900, height=600, min_width=700, min_height=480)

@app.event.on
def greet(name):
    return "Hello, %s!" % name

app.run()