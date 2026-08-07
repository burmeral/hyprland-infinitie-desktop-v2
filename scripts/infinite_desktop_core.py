import sys, struct, threading, time, subprocess, json, os
import fcntl
import select
import math
from evdev import InputDevice, list_devices, ecodes

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hypr_ipc import move_window_exact_lua, move_window_exact, batch_async
from snap import SnapManager

#ruta deseada /home/usuario/scripts/

# Ya no se pasan rutas de dispositivo a mano: se autodetectan por capacidades.
# Uso: infinite_desktop_core.py [speed]
speed = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0

DEVICE_RESCAN_INTERVAL = 3.0  # segundos entre escaneos de nuevos/removidos dispositivos

EVENT_SIZE = struct.calcsize('llHHi')
EV_KEY=1; EV_REL=2; REL_X=0; REL_Y=1
KEY_LEFTMETA=125; KEY_RIGHTMETA=126
KEY_LEFTALT=56; KEY_RIGHTALT=100
KEY_LEFTCTRL=29; KEY_RIGHTCTRL=97
KEY_LEFT=105; KEY_RIGHT=106
KEY_UP=103; KEY_DOWN=108
BTN_LEFT=272

RUNTIME_DIR = os.environ.get("INFINITE_DESKTOP_RUNTIME_DIR",
                             os.path.join(os.environ["XDG_RUNTIME_DIR"], "hyprland-infinite-desktop"))
os.makedirs(RUNTIME_DIR, mode=0o700, exist_ok=True)
STATE_FILE = os.path.join(RUNTIME_DIR, "state")

lock = threading.Lock()
super_pressed=False; alt_pressed=False; ctrl_pressed=False; btn_left=False
modifier_devices = {"super": set(), "alt": set(), "ctrl": set()}
acc_x=0.0; acc_y=0.0
canvas_drag_active=False

# Variables para arrastre de ventanas
window_drag_active = False
dragged_window_addr = None
snap_manager = SnapManager()
last_window_pos = None
last_window_bounds = None
mouse_rel_x = 0
mouse_rel_y = 0

# Paso de movimiento con teclado
KEY_MOVE_STEP = 20

def read_inverted():
    try:
        with open(STATE_FILE) as f:
            return f.read().strip() == 'inverse'
    except:
        return False

def get_monitor_bounds():
    try:
        r = subprocess.run(['hyprctl', 'monitors', '-j'], capture_output=True, text=True, timeout=0.1)
        monitors = json.loads(r.stdout)
        if monitors:
            for m in monitors:
                if m.get('focused', False):
                    return {
                        'left': m['x'],
                        'right': m['x'] + m['width'],
                        'top': m['y'],
                        'bottom': m['y'] + m['height'],
                        'width': m['width'],
                        'height': m['height']
                    }
            m = monitors[0]
            return {
                'left': m['x'],
                'right': m['x'] + m['width'],
                'top': m['y'],
                'bottom': m['y'] + m['height'],
                'width': m['width'],
                'height': m['height']
            }
    except:
        pass
    return {'left': 0, 'right': 1920, 'top': 0, 'bottom': 1080, 'width': 1920, 'height': 1080}

def get_floating_windows(workspace_id):
    try:
        r = subprocess.run(['hyprctl', 'clients', '-j'], capture_output=True, text=True, timeout=0.1)
        clients = json.loads(r.stdout)
        floating = []
        for w in clients:
            if w.get('floating') and w.get('workspace', {}).get('id') == workspace_id:
                floating.append(w)
        return floating
    except:
        return []

def get_focused_window():
    try:
        r = subprocess.run(['hyprctl', 'activewindow', '-j'], capture_output=True, text=True, timeout=0.1)
        return json.loads(r.stdout)
    except:
        return None


def pointer_is_on_workspace_background():
    """Return true when the pointer is not over a client on this workspace."""
    try:
        cursor = subprocess.run(
            ['hyprctl', 'cursorpos', '-j'],
            capture_output=True, text=True, timeout=1.0, check=True
        )
        clients_result = subprocess.run(
            ['hyprctl', 'clients', '-j'],
            capture_output=True, text=True, timeout=1.0, check=True
        )
        workspace_result = subprocess.run(
            ['hyprctl', 'activeworkspace', '-j'],
            capture_output=True, text=True, timeout=1.0, check=True
        )
        position = json.loads(cursor.stdout)
        clients = json.loads(clients_result.stdout)
        workspace_id = json.loads(workspace_result.stdout)['id']
        pointer_x, pointer_y = position['x'], position['y']
        for window in clients:
            if window.get('workspace', {}).get('id') != workspace_id:
                continue
            if window.get('hidden') or not window.get('mapped', True):
                continue
            x, y = window.get('at', [0, 0])
            width, height = window.get('size', [0, 0])
            if x <= pointer_x < x + width and y <= pointer_y < y + height:
                return False
        return True
    except Exception as error:
        # A failed hit test must never turn a window drag into a canvas drag.
        print(f"Background hit test failed: {error}", flush=True)
        return False


def get_window_center(window):
    return (window['at'][0] + window['size'][0] // 2,
            window['at'][1] + window['size'][1] // 2)

def get_window_bounds(window):
    x, y = window['at'][0], window['at'][1]
    w, h = window['size'][0], window['size'][1]
    return {
        'left': x,
        'right': x + w,
        'top': y,
        'bottom': y + h,
        'center_x': x + w // 2,
        'center_y': y + h // 2
    }

def windows_overlap_horizontally(bounds1, bounds2):
    return not (bounds1['right'] <= bounds2['left'] or bounds1['left'] >= bounds2['right'])

def windows_overlap_vertically(bounds1, bounds2):
    return not (bounds1['bottom'] <= bounds2['top'] or bounds1['top'] >= bounds2['bottom'])



def pan_other_windows(excluded_addr, dx, dy, workspace_id):
    """Mueve todas las ventanas EXCEPTO la especificada en un solo batch"""
    if dx == 0 and dy == 0:
        return
    try:
        floating_windows = get_floating_windows(workspace_id)
        exprs = []
        for w in floating_windows:
            if w['address'] != excluded_addr:
                nx = int(w['at'][0] + dx)
                ny = int(w['at'][1] + dy)
                exprs.append(move_window_exact_lua(nx, ny, w['address']))
        batch_async(exprs)
    except:
        pass

def get_monitor_center():
    """Devuelve el centro del monitor enfocado."""
    try:
        r = subprocess.run(['hyprctl', 'monitors', '-j'], capture_output=True, text=True, timeout=0.1)
        monitors = json.loads(r.stdout)
        for m in monitors:
            if m.get('focused', False):
                return m['x'] + m['width'] // 2, m['y'] + m['height'] // 2
    except:
        pass
    return 960, 540


def monitor_window_drag():
    """Monitorea si se esta arrastrando una ventana y aplica empuje en bordes"""
    global window_drag_active, dragged_window_addr, last_window_bounds, mouse_rel_x, mouse_rel_y
    
    while True:
        try:
            with lock:
                is_dragging = (super_pressed and btn_left and not alt_pressed and not ctrl_pressed and not canvas_drag_active)
                mouse_dx = mouse_rel_x
                mouse_dy = mouse_rel_y
                mouse_rel_x = 0
                mouse_rel_y = 0
            
            if is_dragging and not window_drag_active:
                focused = get_focused_window()
                if focused and focused.get('address'):
                    dragged_window_addr = focused['address']
                    window_drag_active = True
                    last_window_bounds = get_window_bounds(focused)
                    snap_manager.begin_drag(dragged_window_addr)
            
            elif not is_dragging and window_drag_active:
                snap_manager.end_drag(dragged_window_addr)
                window_drag_active = False
                dragged_window_addr = None
                last_window_bounds = None
            
            if window_drag_active and dragged_window_addr:
                window = get_focused_window()
                if window and window.get('address') == dragged_window_addr:
                    current_bounds = get_window_bounds(window)
                    snap_manager.update_drag(dragged_window_addr)
                    monitor = get_monitor_bounds()
                    MARGIN = 10
                    
                    touch_left   = current_bounds['left']   <= monitor['left']   + MARGIN
                    touch_right  = current_bounds['right']  >= monitor['right']  - MARGIN
                    touch_top    = current_bounds['top']    <= monitor['top']    + MARGIN
                    touch_bottom = current_bounds['bottom'] >= monitor['bottom'] - MARGIN
                    
                    if (touch_left or touch_right or touch_top or touch_bottom) and (mouse_dx != 0 or mouse_dy != 0):
                        pan_dx = 0
                        pan_dy = 0
                        
                        if touch_right and mouse_dx > 0:
                            pan_dx = -mouse_dx
                        elif touch_left and mouse_dx < 0:
                            pan_dx = -mouse_dx
                        
                        if touch_bottom and mouse_dy > 0:
                            pan_dy = -mouse_dy
                        elif touch_top and mouse_dy < 0:
                            pan_dy = -mouse_dy
                        
                        if pan_dx != 0 or pan_dy != 0:
                            r = subprocess.run(['hyprctl', 'activeworkspace', '-j'], 
                                             capture_output=True, text=True, timeout=0.1)
                            ws = json.loads(r.stdout)
                            workspace_id = ws['id']
                            pan_other_windows(dragged_window_addr, int(pan_dx), int(pan_dy), workspace_id)
                    
                    last_window_bounds = current_bounds
                else:
                    snap_manager.end_drag(dragged_window_addr)
                    window_drag_active = False
                    dragged_window_addr = None
            
            time.sleep(0.016)
        except Exception as e:
            time.sleep(0.1)


def move_active_window(direction):
    """Mueve la ventana activa KEY_MOVE_STEP px en la direccion indicada.
    Si toca el borde del monitor, empuja las demas ventanas en sentido contrario."""
    try:
        r = subprocess.run(['hyprctl', 'activeworkspace', '-j'], capture_output=True, text=True, timeout=0.1)
        ws = json.loads(r.stdout)
        workspace_id = ws['id']

        window = get_focused_window()
        if not window or not window.get('floating'):
            return

        monitor = get_monitor_bounds()
        bounds = get_window_bounds(window)
        addr = window['address']

        dx, dy = 0, 0
        if direction == 'left':
            dx = -KEY_MOVE_STEP
        elif direction == 'right':
            dx = KEY_MOVE_STEP
        elif direction == 'up':
            dy = -KEY_MOVE_STEP
        elif direction == 'down':
            dy = KEY_MOVE_STEP

        new_x = window['at'][0] + dx
        new_y = window['at'][1] + dy

        # Detectar si toca borde DESPUÉS del movimiento
        new_bounds_left   = new_x
        new_bounds_right  = new_x + window['size'][0]
        new_bounds_top    = new_y
        new_bounds_bottom = new_y + window['size'][1]

        hits_left   = new_bounds_left   <= monitor['left']
        hits_right  = new_bounds_right  >= monitor['right']
        hits_top    = new_bounds_top    <= monitor['top']
        hits_bottom = new_bounds_bottom >= monitor['bottom']

        hitting_edge = (dx < 0 and hits_left) or (dx > 0 and hits_right) or                        (dy < 0 and hits_top)  or (dy > 0 and hits_bottom)

        # Mover la ventana activa
        move_window_exact(new_x, new_y, addr, timeout=0.2)

        # Si toca borde, empujar las demas en sentido contrario
        if hitting_edge:
            pan_other_windows(addr, -dx, -dy, workspace_id)

    except Exception as e:
        print(f"Error en move_active_window: {e}", flush=True)


def classify_device(path):
    """Devuelve 'mouse', 'keyboard', 'touchpad', o None segun las capacidades reales del dispositivo,
    sin importar el nombre/marca. Esto es lo que permite que funcione con cualquier
    mouse o teclado (alambrico, inalambrico, el que sea)."""
    try:
        dev = InputDevice(path)
        caps = dev.capabilities()
        dev.close()
    except Exception:
        return None

    keys = set(caps.get(ecodes.EV_KEY, []))
    rels = set(caps.get(ecodes.EV_REL, []))
    abss = {code for code, _ in caps.get(ecodes.EV_ABS, [])}

    is_mouse = (ecodes.REL_X in rels and ecodes.REL_Y in rels and ecodes.BTN_LEFT in keys)
    if is_mouse:
        return 'mouse'
  
    is_touchpad = (
        ecodes.ABS_X in abss and ecodes.ABS_Y in abss
        and ecodes.BTN_TOUCH in keys
        and ecodes.BTN_LEFT in keys
        and ecodes.REL_X not in rels  # exclude mice that also report ABS
    )
  
    if is_touchpad:
        return 'touchpad'

    # Un teclado "real" tiene el rango completo de teclas alfanumericas y las teclas Meta,
    # esto excluye las interfaces auxiliares (Consumer Control, System Control) que
    # muchos recievers 2.4G tambien exponen.
    is_keyboard = (
        ecodes.KEY_A in keys and ecodes.KEY_Z in keys and ecodes.KEY_LEFTSHIFT in keys
        and (ecodes.KEY_LEFTMETA in keys or ecodes.KEY_RIGHTMETA in keys)
    )
  
    if is_keyboard:
        return 'keyboard'

    return None


def scan_devices():
    keyboards, mice, touchpads = [], [], []
    for path in list_devices():
        kind = classify_device(path)
        if kind == 'mouse':
            mice.append(path)
        elif kind == 'keyboard':
            keyboards.append(path)
        elif kind == 'touchpad':
            touchpads.append(path)
    return keyboards, mice, touchpads



def kbd_reader_device(path):
    """Read one keyboard and clear only its modifier state on disconnect."""
    global super_pressed, alt_pressed, ctrl_pressed
    try:
        fd = open(path, 'rb')
    except Exception:
        return

    while True:
        try:
            data = fd.read(EVENT_SIZE)
        except Exception:
            break
        if not data or len(data) < EVENT_SIZE:
            break
        _, _, etype, code, value = struct.unpack('llHHi', data)
        if etype != EV_KEY or value == 2:
            continue

        with lock:
            if code in (KEY_LEFTMETA, KEY_RIGHTMETA):
                devices = modifier_devices["super"]
            elif code in (KEY_LEFTALT, KEY_RIGHTALT):
                devices = modifier_devices["alt"]
            elif code in (KEY_LEFTCTRL, KEY_RIGHTCTRL):
                devices = modifier_devices["ctrl"]
            else:
                continue
            if value == 1:
                devices.add(path)
            else:
                devices.discard(path)
            super_pressed = bool(modifier_devices["super"])
            alt_pressed = bool(modifier_devices["alt"])
            ctrl_pressed = bool(modifier_devices["ctrl"])

    try:
        fd.close()
    except Exception:
        pass
    with lock:
        for devices in modifier_devices.values():
            devices.discard(path)
        super_pressed = bool(modifier_devices["super"])
        alt_pressed = bool(modifier_devices["alt"])
        ctrl_pressed = bool(modifier_devices["ctrl"])



def mouse_reader_device(path):
    """Read one mouse and pan only a Super-drag started on empty background."""
    global acc_x, acc_y, btn_left, mouse_rel_x, mouse_rel_y, canvas_drag_active
    try:
        fd = open(path, 'rb')
    except Exception:
        return

    while True:
        try:
            data = fd.read(EVENT_SIZE)
        except Exception:
            break
        if not data or len(data) < EVENT_SIZE:
            break
        _, _, etype, code, value = struct.unpack('llHHi', data)

        if etype == EV_KEY and code == BTN_LEFT:
            if value == 1:
                with lock:
                    can_start = super_pressed and not alt_pressed and not ctrl_pressed
                starts_on_background = can_start and pointer_is_on_workspace_background()
                with lock:
                    btn_left = True
                    canvas_drag_active = starts_on_background
                    acc_x = 0.0
                    acc_y = 0.0
            elif value == 0:
                with lock:
                    btn_left = False
                    canvas_drag_active = False
                    acc_x = 0.0
                    acc_y = 0.0
            continue

        if etype != EV_REL:
            continue
        with lock:
            if code == REL_X:
                mouse_rel_x += value
            elif code == REL_Y:
                mouse_rel_y += value

            if canvas_drag_active and super_pressed and btn_left:
                sign = -1 if read_inverted() else 1
                if code == REL_X:
                    acc_x += value * speed * sign
                elif code == REL_Y:
                    acc_y += value * speed * sign
            else:
                acc_x = 0.0
                acc_y = 0.0

    try:
        fd.close()
    except Exception:
        pass
    with lock:
        btn_left = False
        canvas_drag_active = False
        acc_x = 0.0
        acc_y = 0.0

def touchpad_reader_device(path):
    """Read one touchpad and feed deltas into acc_x/acc_y for canvas drag."""
    global acc_x, acc_y, btn_left, mouse_rel_x, mouse_rel_y, canvas_drag_active

    try:
        dev = InputDevice(path)
        abs_info_x = dev.absinfo(ecodes.ABS_X)
        abs_info_y = dev.absinfo(ecodes.ABS_Y)
        tp_max_x = abs_info_x.maximum or 1572
        tp_max_y = abs_info_y.maximum or 984
    except Exception:
        return

    monitor = get_monitor_bounds()
    scale_x = monitor['width'] / tp_max_x
    scale_y = monitor['height'] / tp_max_y

    prev_x = None
    prev_y = None
    cur_x = None
    cur_y = None
    finger_down = False

    try:
        for event in dev.read_loop():
            if event.type == ecodes.EV_KEY:
                if event.code == ecodes.BTN_LEFT:
                    if event.value == 1:
                        with lock:
                            can_start = super_pressed and not alt_pressed and not ctrl_pressed
                        starts_on_background = can_start and pointer_is_on_workspace_background()
                        with lock:
                            btn_left = True
                            canvas_drag_active = starts_on_background
                            acc_x = 0.0
                            acc_y = 0.0
                    elif event.value == 0:
                        with lock:
                            btn_left = False
                            canvas_drag_active = False
                            acc_x = 0.0
                            acc_y = 0.0
                elif event.code == ecodes.BTN_TOUCH:
                    finger_down = bool(event.value)
                    if not finger_down:
                        prev_x = None
                        prev_y = None

            elif event.type == ecodes.EV_ABS:
                if event.code == ecodes.ABS_X:
                    cur_x = event.value
                elif event.code == ecodes.ABS_Y:
                    cur_y = event.value

            elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
                if not finger_down or cur_x is None or cur_y is None:
                    prev_x = cur_x
                    prev_y = cur_y
                    continue

                if prev_x is not None and prev_y is not None:
                    raw_dx = cur_x - prev_x
                    raw_dy = cur_y - prev_y
                    dx = raw_dx * scale_x * speed
                    dy = raw_dy * scale_y * speed

                    with lock:
                        mouse_rel_x += int(raw_dx * scale_x)
                        mouse_rel_y += int(raw_dy * scale_y)
                        if canvas_drag_active and super_pressed and btn_left:
                            sign = -1 if read_inverted() else 1
                            acc_x += dx * sign
                            acc_y += dy * sign

                prev_x = cur_x
                prev_y = cur_y

    except Exception:
        pass
    finally:
        try:
            dev.close()
        except Exception:
            pass
        with lock:
            btn_left = False
            canvas_drag_active = False
            acc_x = 0.0
            acc_y = 0.0

_active_kbd_threads = {}
_active_mouse_threads = {}

def device_manager():
    """Escanea periodicamente /dev/input buscando teclados y mouses nuevos
    (o reconectados) y lanza un hilo lector para cada uno. Si un dispositivo
    se desconecta, su hilo simplemente termina solo al fallar el read().

    Los primeros segundos (WARMUP_DURATION) escanea mucho mas seguido
    (WARMUP_INTERVAL) porque justo al iniciar sesion, dongles inalambricos
    a veces tardan unos segundos en terminar de enumerar sus interfaces USB.
    Despues de ese periodo baja al intervalo normal para no gastar CPU."""
    WARMUP_DURATION = 20.0
    WARMUP_INTERVAL = 0.5
    start_time = time.time()

    while True:
        try:
            keyboards, mice, touchpads = scan_devices()

            for path in keyboards:
                t = _active_kbd_threads.get(path)
                if t is None or not t.is_alive():
                    nt = threading.Thread(target=kbd_reader_device, args=(path,), daemon=True)
                    nt.start()
                    _active_kbd_threads[path] = nt
                    print(f"[+] Teclado detectado: {path}", flush=True)

            for path in mice:
                t = _active_mouse_threads.get(path)
                if t is None or not t.is_alive():
                    nt = threading.Thread(target=mouse_reader_device, args=(path,), daemon=True)
                    nt.start()
                    _active_mouse_threads[path] = nt
                    print(f"[+] Mouse detectado: {path}", flush=True)

             
            for path in touchpads:
                t = _active_mouse_threads.get(path)
                if t is None or not t.is_alive():
                    nt = threading.Thread(target=touchpad_reader_device, args=(path,), daemon=True)
                    nt.start()
                    _active_mouse_threads[path] = nt
                    print(f"[+] Touchpad detectado: {path}", flush=True)
                  
        except Exception as e:
            print(f"Error en device_manager: {e}", flush=True)

        elapsed = time.time() - start_time
        interval = WARMUP_INTERVAL if elapsed < WARMUP_DURATION else DEVICE_RESCAN_INTERVAL
        time.sleep(interval)

# PRECARGAR
print("Precargando...", flush=True)
try:
    subprocess.run(['hyprctl', 'activeworkspace', '-j'], capture_output=True, text=True, timeout=0.5)
    subprocess.run(['hyprctl', 'clients', '-j'], capture_output=True, text=True, timeout=0.5)
except:
    pass

snap_manager.start()
threading.Thread(target=device_manager, daemon=True).start()
threading.Thread(target=monitor_window_drag, daemon=True).start()
print("Infinite Desktop activo (deteccion automatica de dispositivos)", flush=True)
print("Super+drag window: Mover ventana (los bordes empujan el lienzo)", flush=True)
print("Super+drag background: Arrastrar todo el escritorio", flush=True)
print("Super+flechas: Navegacion via hyprland bind", flush=True)
print("Super+Shift+flechas: Mover ventana activa via hyprland bind", flush=True)

# Caché del workspace activo (se refresca cada 2s para no llamar hyprctl cada frame)
_cached_workspace_id = None
_last_workspace_check = 0
WORKSPACE_CACHE_TTL = 2.0

def get_cached_workspace_id():
    global _cached_workspace_id, _last_workspace_check
    now = time.time()
    if _cached_workspace_id is None or (now - _last_workspace_check) > WORKSPACE_CACHE_TTL:
        try:
            r = subprocess.run(['hyprctl', 'activeworkspace', '-j'],
                               capture_output=True, text=True, timeout=0.1)
            ws = json.loads(r.stdout)
            _cached_workspace_id = ws['id']
            _last_workspace_check = now
        except:
            pass
    return _cached_workspace_id

# Loop principal para arrastre de escritorio
while True:
    time.sleep(0.016)

    with lock:
        active_drag = canvas_drag_active and super_pressed and btn_left
        dx = acc_x
        dy = acc_y
        acc_x = 0.0
        acc_y = 0.0

    if not active_drag:
        continue

    idx = int(round(dx))
    idy = int(round(dy))

    if idx == 0 and idy == 0:
        continue

    try:
        workspace_id = get_cached_workspace_id()
        if workspace_id is None:
            continue

        r = subprocess.run(['hyprctl', 'clients', '-j'], capture_output=True, text=True, timeout=0.1)
        clients = json.loads(r.stdout)

        exprs = []
        for w in clients:
            if w.get('floating') and w.get('workspace', {}).get('id') == workspace_id:
                nx = w['at'][0] + idx
                ny = w['at'][1] + idy
                exprs.append(move_window_exact_lua(nx, ny, w['address']))

        batch_async(exprs)
    except Exception as e:
        pass
