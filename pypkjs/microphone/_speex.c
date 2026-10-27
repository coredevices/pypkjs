/* Wideband Speex for watch microphone audio: a decoder, as the phone app uses, and an encoder
 * matching the watch's, for tests. */

#define PY_SSIZE_T_CLEAN
#include <Python.h>

#include <speex/speex.h>

typedef struct {
    PyObject_HEAD
    void *state;
    SpeexBits bits;
    int frame_size;
} Codec;

static void codec_dealloc(Codec *self, void (*destroy)(void *)) {
    if (self->state) {
        speex_bits_destroy(&self->bits);
        destroy(self->state);
    }
    Py_TYPE(self)->tp_free((PyObject *)self);
}

/* Decoder */

static int decoder_init(Codec *self, PyObject *args, PyObject *kwds) {
    static char *kwlist[] = {NULL};
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "", kwlist)) {
        return -1;
    }
    self->state = speex_decoder_init(&speex_wb_mode);
    if (!self->state) {
        PyErr_NoMemory();
        return -1;
    }
    speex_bits_init(&self->bits);
    speex_decoder_ctl(self->state, SPEEX_GET_FRAME_SIZE, &self->frame_size);
    return 0;
}

static void decoder_dealloc(Codec *self) {
    codec_dealloc(self, speex_decoder_destroy);
}

static PyObject *decoder_decode(Codec *self, PyObject *arg) {
    Py_buffer frame;
    if (PyObject_GetBuffer(arg, &frame, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    PyObject *pcm = PyBytes_FromStringAndSize(NULL, self->frame_size * sizeof(spx_int16_t));
    if (!pcm) {
        PyBuffer_Release(&frame);
        return NULL;
    }
    speex_bits_read_from(&self->bits, frame.buf, (int)frame.len);
    int result = speex_decode_int(self->state, &self->bits, (spx_int16_t *)PyBytes_AS_STRING(pcm));
    PyBuffer_Release(&frame);
    if (result != 0) {
        Py_DECREF(pcm);
        PyErr_SetString(PyExc_ValueError, "corrupt Speex frame");
        return NULL;
    }
    return pcm;
}

static PyMethodDef decoder_methods[] = {
    {"decode", (PyCFunction)decoder_decode, METH_O,
     "decode(frame) -> bytes: one frame of 16-bit native-endian mono PCM."},
    {NULL},
};

/* Encoder */

static int encoder_init(Codec *self, PyObject *args, PyObject *kwds) {
    static char *kwlist[] = {"quality", NULL};
    int quality = 6;
    if (!PyArg_ParseTupleAndKeywords(args, kwds, "|i", kwlist, &quality)) {
        return -1;
    }
    self->state = speex_encoder_init(&speex_wb_mode);
    if (!self->state) {
        PyErr_NoMemory();
        return -1;
    }
    speex_bits_init(&self->bits);
    speex_encoder_ctl(self->state, SPEEX_SET_QUALITY, &quality);
    speex_encoder_ctl(self->state, SPEEX_GET_FRAME_SIZE, &self->frame_size);
    return 0;
}

static void encoder_dealloc(Codec *self) {
    codec_dealloc(self, speex_encoder_destroy);
}

static PyObject *encoder_encode(Codec *self, PyObject *arg) {
    Py_buffer pcm;
    if (PyObject_GetBuffer(arg, &pcm, PyBUF_SIMPLE) < 0) {
        return NULL;
    }
    if (pcm.len != (Py_ssize_t)(self->frame_size * sizeof(spx_int16_t))) {
        PyErr_Format(PyExc_ValueError, "expected %d samples", self->frame_size);
        PyBuffer_Release(&pcm);
        return NULL;
    }
    /* speex_encode_int scales its input in place. */
    spx_int16_t samples[640];
    memcpy(samples, pcm.buf, pcm.len);
    PyBuffer_Release(&pcm);
    speex_bits_reset(&self->bits);
    speex_encode_int(self->state, samples, &self->bits);
    char out[200];
    int len = speex_bits_write(&self->bits, out, sizeof(out));
    return PyBytes_FromStringAndSize(out, len);
}

static PyMethodDef encoder_methods[] = {
    {"encode", (PyCFunction)encoder_encode, METH_O,
     "encode(pcm) -> bytes: one frame of 16-bit native-endian mono PCM, encoded."},
    {NULL},
};

static PyObject *codec_frame_size(Codec *self, void *closure) {
    return PyLong_FromLong(self->frame_size);
}

static PyGetSetDef codec_getset[] = {
    {"frame_size", (getter)codec_frame_size, NULL, "Samples per frame.", NULL},
    {NULL},
};

static PyTypeObject DecoderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pypkjs.microphone._speex.Decoder",
    .tp_doc = "Wideband (16 kHz) Speex decoder.",
    .tp_basicsize = sizeof(Codec),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_init = (initproc)decoder_init,
    .tp_dealloc = (destructor)decoder_dealloc,
    .tp_methods = decoder_methods,
    .tp_getset = codec_getset,
};

static PyTypeObject EncoderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "pypkjs.microphone._speex.Encoder",
    .tp_doc = "Wideband (16 kHz) Speex encoder.",
    .tp_basicsize = sizeof(Codec),
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_new = PyType_GenericNew,
    .tp_init = (initproc)encoder_init,
    .tp_dealloc = (destructor)encoder_dealloc,
    .tp_methods = encoder_methods,
    .tp_getset = codec_getset,
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "pypkjs.microphone._speex",
    .m_doc = "Wideband Speex codec.",
    .m_size = -1,
};

PyMODINIT_FUNC PyInit__speex(void) {
    if (PyType_Ready(&DecoderType) < 0 || PyType_Ready(&EncoderType) < 0) {
        return NULL;
    }
    PyObject *m = PyModule_Create(&module);
    if (!m) {
        return NULL;
    }
    Py_INCREF(&DecoderType);
    Py_INCREF(&EncoderType);
    if (PyModule_AddObject(m, "Decoder", (PyObject *)&DecoderType) < 0 ||
        PyModule_AddObject(m, "Encoder", (PyObject *)&EncoderType) < 0) {
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
