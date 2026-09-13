# Production path corrected from OCR

The lesson series must teach the final approve-to-execute path as asynchronous: approval writes the resume decision into the checkpoint, writes run-state as queued, enqueues SQS FIFO, and returns `202 queued`; a worker later resumes with `None`. The older synchronous approval stream is only a legacy/interim path and should not be the default mental model.
