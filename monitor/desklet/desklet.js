const Desklet = imports.ui.desklet;
const St = imports.gi.St;
const GLib = imports.gi.GLib;
const Mainloop = imports.mainloop;
const Settings = imports.ui.settings;

const STATUS_URL = "http://127.0.0.1:11436/api/text";

// GJS отдаёт байты curl как есть; String.fromCharCode трактует их как Latin-1,
// поэтому UTF-8 (кириллица, «·», «°») декодируем вручную.
function utf8(bytes) {
    let s = "";
    for (let i = 0; i < bytes.length; i++) {
        let c = bytes[i];
        if (c < 0x80) {
            s += String.fromCharCode(c);
        } else if (c > 0xbf && c < 0xe0) {
            s += String.fromCharCode(((c & 0x1f) << 6) | (bytes[++i] & 0x3f));
        } else if (c > 0xdf && c < 0xf0) {
            s += String.fromCharCode(
                ((c & 0x0f) << 12) | ((bytes[++i] & 0x3f) << 6) | (bytes[++i] & 0x3f)
            );
        }
    }
    return s;
}

function LmintaiDesklet(metadata, desklet_id) {
    this._init(metadata, desklet_id);
}

LmintaiDesklet.prototype = {
    __proto__: Desklet.Desklet.prototype,

    _init: function(metadata, desklet_id) {
        Desklet.Desklet.prototype._init.call(this, metadata, desklet_id);

        this.interval = 2;
        this._timeout = null;
        this.settings = new Settings.DeskletSettings(this, metadata.uuid, desklet_id);
        this.settings.bind("interval", "interval", this._reschedule);

        this._box = new St.BoxLayout({ vertical: true, style_class: "lmintai-box" });
        this._labels = [];
        for (let i = 0; i < 4; i++) {
            // Цвета задаём inline: тема Cinnamon перебивает color из stylesheet.css.
            let style = i === 0
                ? "font-size: 13px; font-weight: bold; color: #8b95a5; margin-bottom: 2px;"
                : "font-size: 11px; color: #b8bfcb; font-family: 'DejaVu Sans Mono', monospace;";
            let label = new St.Label({ text: "", style: style });
            this._labels.push(label);
            this._box.add(label);
        }
        this.setContent(this._box);

        this._refresh();
        this._reschedule();
    },

    _reschedule: function() {
        if (this._timeout) {
            Mainloop.source_remove(this._timeout);
            this._timeout = null;
        }
        this._timeout = Mainloop.timeout_add_seconds(this.interval, () => {
            this._refresh();
            return true;
        });
    },

    _refresh: function() {
        let lines = ["нет данных", "проверьте: systemctl --user status lmintai-status", "—", "—"];
        try {
            let [ok, out] = GLib.spawn_command_line_sync("curl -s -m 1 " + STATUS_URL);
            let text = utf8(out);
            if (ok && text.trim()) {
                lines = text.trim().split("\n");
            }
        } catch (e) {
            // сервер не запущен — оставляем подсказку
        }
        for (let i = 0; i < this._labels.length; i++) {
            this._labels[i].set_text(lines[i] || "");
        }
        this._colorize(lines[0] || "");
    },

    _colorize: function(head) {
        let color = "#8b95a5";
        if (head.indexOf("загрузка") === 0 || head.indexOf("очередь") === 0) {
            color = "#e0b341";
        } else if (head.indexOf("генерация") === 0) {
            color = "#4cc38a";
        } else if (head.indexOf("bastion") === 0 || head.indexOf("нет данных") === 0) {
            color = "#e5484d";
        }
        this._labels[0].style = "font-size: 13px; font-weight: bold; margin-bottom: 2px; color: " + color + ";";
    },

    on_desklet_removed: function() {
        if (this._timeout) {
            Mainloop.source_remove(this._timeout);
            this._timeout = null;
        }
        this.settings.finalize();
    }
};

function main(metadata, desklet_id) {
    return new LmintaiDesklet(metadata, desklet_id);
}
