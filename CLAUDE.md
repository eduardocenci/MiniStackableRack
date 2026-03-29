# MiniStackableRack

## Folder Structure

**The folder hierarchy must mirror the system architecture.** A component that runs inside another component lives in a subfolder of it — not alongside it.

```
Root
├── 3d-models/                  3D print files for the physical rack enclosure
├── gitignore/                  Local-only files, never committed (gitignored)
└── scripts/
    ├── proxmox/                MiniPC runs Proxmox (hypervisor)
    │   └── homeassistant/      Home Assistant OS runs as a VM on Proxmox
    │       ├── bnu-homeassistant/
    │       ├── ply-homeassistant/
    │       └── bg-homeassistant/
    └── raspberry-pi/           Raspberry Pi — independent rack component (not a VM)
        └── bnu-raspberrypi/
```

Each deployment instance is named `<deployment>-<component>` (e.g. `bnu-homeassistant`, `ply-proxmox`).

## Credentials

- Store all credentials in `.env` files local to the relevant component folder
- `.env` is gitignored — never committed
- The `gitignore/` folder is also gitignored and can hold any other local-only files

## Rules

- Before adding a new script or config, place it under the component it belongs to
- If a component runs inside another (VM, container, add-on), its folder goes inside the parent's folder
- Independent rack components (Raspberry Pi, Remote KVM, Zigbee Gateway) sit at the top level of `scripts/`
