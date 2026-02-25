"""
Drone Perception System
=======================
Civilian use only: Search & Rescue, Inspection, Situational Awareness.

Privacy-by-design: faces blurred by default, no weaponisation features.
Real-time: 30 FPS on Jetson Orin Nano via TensorRT FP16.

Classes: human_presence (3) | animal (4) | vehicle (9) | drone (7) = 23 total
Architecture: YOLOv9-S single multi-task detector + ByteTrack MOT

Version: 1.0.0
"""

__version__ = "1.0.0"
