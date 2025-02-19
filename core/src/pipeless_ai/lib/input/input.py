import sys
import traceback
import numpy as np

import gi
gi.require_version('GLib', '2.0')
gi.require_version('GObject', '2.0')
gi.require_version('Gst', '1.0')
gi.require_version('GstApp', '1.0')
from gi.repository import Gst, GObject, GstApp, GLib

from pipeless_ai.lib.logger import logger, update_logger_component
from pipeless_ai.lib.connection import InputOutputSocket, InputPushSocket
from pipeless_ai.lib.config import Config
from pipeless_ai.lib.messages import EndOfStreamMsg, RgbImageMsg, StreamCapsMsg, StreamTagsMsg

def on_new_sample(sink: GstApp.AppSink) -> Gst.FlowReturn:
    sample = sink.pull_sample()
    if sample is None:
        logger.error('Sample is None!')
        return Gst.FlowReturn.ERROR # TODO: We should return a different status if we want to leave the app running forever and being able to recover from flows

    logger.debug(f'Sample caps: {sample.get_caps()}')

    buffer = sample.get_buffer()
    if buffer is None:
        logger.error('Buffer is None!')
        return Gst.FlowReturn.ERROR

    # Get multimedia data from the buffer
    success, info = buffer.map(Gst.MapFlags.READ)
    if not success:
        logger.error('Getting multimedia data from the buffer did not success.')
        return Gst.FlowReturn.ERROR

    caps = sample.get_caps()
    width = caps.get_structure(0).get_value("width")
    height = caps.get_structure(0).get_value("height")
    dts = buffer.dts
    pts = buffer.pts
    duration = buffer.duration
    ndframe = np.ndarray(
        shape=(height, width, 3),
        dtype=np.uint8, buffer=info.data
    )

    msg = RgbImageMsg(width, height, ndframe, dts, pts, duration)
    s_msg = msg.serialize()

    # Pass msg to the workers
    s_push = InputPushSocket()
    s_push.send(s_msg)

    # Release resources
    buffer.unmap(info)

    return Gst.FlowReturn.OK

def on_bus_message(bus: Gst.Bus, msg: Gst.Message, loop: GObject.MainLoop):
    """
    Callback to manage bus messages
    For example, when we receive a new-sample and return an error from
    the processing, we can catch it and stop the pipeline here.
    """
    mtype = msg.type
    if mtype == Gst.MessageType.EOS:
        logger.info("End of stream reached.")
        m_socket = InputOutputSocket('w')
        w_socket = InputPushSocket()
        m_msg = EndOfStreamMsg()
        m_msg = m_msg.serialize()
        logger.debug('Notifying EOS to output')
        m_socket.send(m_msg) # Notify output
        config = Config(None)
        for _ in range(config.get_n_workers()):
            # The socket is round robin, send to all workers
            # TODO: a broadcast socket for this is better for scaling
            #       by saving the n_workers config option
            logger.info('Notifying EOS to worker')
            w_socket.ensure_send(m_msg)
    elif mtype == Gst.MessageType.ERROR:
        err, debug = msg.parse_error()
        logger.error(f"Error received from element {msg.src.get_name()}: {err.message}")
        logger.error(f"Debugging information: {debug if debug else 'none'}")
        loop.quit()
    elif mtype == Gst.MessageType.WARNING:
        err, debug = msg.parse_warning()
        logger.warning(f"Warning received from element {msg.src.get_name()}: {err.message}")
        logger.warning(f"Debugging information: {debug if debug else 'none'}")
    elif mtype == Gst.MessageType.STATE_CHANGED:
        old_state, new_state, pending_state = msg.parse_state_changed()
        logger.debug(f'New pipeline state: {new_state}')
    elif mtype == Gst.MessageType.TAG:
        tags = msg.parse_tag().to_string()
        logger.info(f'Tags parsed: {tags}')
        t_socket = InputOutputSocket('w')
        t_msg = StreamTagsMsg(tags)
        t_socket.send(t_msg.serialize())

    return True

def on_pad_upstream_event(pad, info, user_data):
    """
     TODO: if the pad name is always different, we can run simultaneous
     pipelines in both input and output by simply creating a create_pipeline
     function and managing several of them in both the input and the output.
     That would allow users to send several streams to the same app.
     they would be processed on the same workers with the same code.
     How do we distinguish the videos for the output? Right now
     they will both be sent to the same URI
    """
    caps = pad.get_current_caps()
    if caps is not None:
        # Caps negotiation is complete, notify new stream to output component
        logger.debug(f'[green]uridecodebin pad "{pad.get_name()}" with caps: {caps}[/green]')
        m_socket = InputOutputSocket('w')
        m_msg = StreamCapsMsg(caps.to_string())
        m_socket.send(m_msg.serialize())
        # We already got the caps. Remove the probe from the pad
        return Gst.PadProbeReturn.REMOVE

    return Gst.PadProbeReturn.OK # Leave the probe in place

def handle_caps_change(pad):
    # Connect an async handler to the pad to be notified when caps are set
    pad.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, on_pad_upstream_event, pad)

def on_pad_added(element, pad, *callbacks):
    for callback in callbacks:
        callback(pad)

def input():
    # Load config
    config = Config(None)
    Gst.init(None)

    update_logger_component('INPUT')

    logger.info(f"Reading video from {config.get_input().get_video().get_uri()}")
    pipeline = Gst.Pipeline.new("pipeline")

    # Create elements
    # We will force RBG on the sink and videoconvert takes care of
    # converting between space colors negotiating caps automatically.
    # Ref: https://gstreamer.freedesktop.org/documentation/tutorials/basic/handy-elements.html?gi-language=c#videoconvert
    uridecodebin = Gst.ElementFactory.make("uridecodebin3", "uridecodebin")
    videoconvert = Gst.ElementFactory.make("videoconvert", "videoconvert")
    appsink = Gst.ElementFactory.make("appsink", "appsink")

    if not pipeline:
        logger.error('Failed to create pipeline')
        sys.exit(1)
    if not uridecodebin:
        logger.error('Failed to create uridecodebin')
        sys.exit(1)
    if not videoconvert:
        logger.error('Failed to create videoconvert')
        sys.exit(1)
    if not appsink:
        logger.error("Failed to create appsink.")
        sys.exit(1)

    # Set properties for elements
    appsink.set_property("emit-signals", True)
    appsink.connect("new-sample", on_new_sample)
    # Force RGB output in sink
    caps = Gst.Caps.from_string("video/x-raw,format=RGB")
    appsink.set_property("caps", caps)

    # Add elemets to the pipeline
    for elem in [uridecodebin, videoconvert, appsink]: pipeline.add(elem)

    # Link static elements (fixed number of pads): uridecoder (linked later) -> videoconvert -> appsink
    if not videoconvert.link(appsink):
        logger.error("Failed to link appsink to videoconvert")
        sys.exit(1)

    videoconvert_sink_pad = videoconvert.get_static_pad("sink")
    # Link dynamic elements (dynamic number of pads)
    # uridecodebin creates pads for each stream found in the uri (ex: video, audio, subtitles)
    uridecodebin.set_property("uri", config.get_input().get_video().get_uri())
    def pad_added_callback(pad):
        if not videoconvert_sink_pad.is_linked():
            logger.info('Linking uridecoderbin pad to videoconvert pad')
            pad.link(videoconvert_sink_pad) # uridecoder -> videoconvert -> appsink
        else:
            logger.warning('Video converter pad is already linked. Skipping uridecoder link')

    uridecodebin.connect(
        "pad-added",
        lambda element, pad:
          on_pad_added(
            element, pad, pad_added_callback, handle_caps_change
        )
    )

    loop = GLib.MainLoop()

    # Handle bus events on the main loop
    bus = pipeline.get_bus()
    bus.add_signal_watch()
    bus.connect("message", on_bus_message, loop)

    logger.info('Starting pipeline')
    ret = pipeline.set_state(Gst.State.PLAYING)
    if ret == Gst.StateChangeReturn.FAILURE:
        logger.error("[red]Unable to set the pipeline to the playing state.[/red]")
        sys.exit(1)

    try:
        logger.debug(f'uridecodebin state: {uridecodebin.get_state(5)}')
        logger.debug(f'videoconverter state: {videoconvert.get_state(5)}')
        logger.debug(f'appsink state: {appsink.get_state(5)}')

        # Start socket to wait all components connections
        s_push  = InputPushSocket() # Listener
        m_socket = InputOutputSocket('w') # Waits for output

        loop.run()
    except KeyboardInterrupt:
        pass
    except Exception:
        traceback.print_exc()
        loop.quit()
    finally:
        logger.info('Closing pipeline')
        pipeline.set_state(Gst.State.NULL)
        logger.info('Pipeline closed')
        # Rettreive and close the sockets
        m_socket = InputOutputSocket('w')
        m_socket.close()
        s_push  = InputPushSocket()
        s_push.close()
