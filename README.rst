MultiSafepay implementation for pretix
========================================

This is a plugin for `pretix`_. 

Development setup
-----------------

1. Make sure that you have a working `pretix development setup`_.

2. Clone this repository, eg to ``local/pretix-multisafepay``.

3. Activate the virtual environment you use for pretix development.

4. Execute ``pip install -e .`` within this directory to register this application with pretix's plugin registry.

5. Execute ``make`` within this directory to compile translations.

6. Restart your local pretix server. You can now use the plugin from this repository for your events by enabling it in
   the 'plugins' tab in the settings.

Docker
------

Since this package is inofficial and not (yet) tracked by PyPI, clone the repository in your Pretix Dockerfile, e.g.
..  code-block:: docker
    :caption: pretix/Dockerfile

    FROM pretix/standalone:stable
    USER root
    RUN pip install --upgrade pip
    RUN git clone https://github.com/bencarp/pretix-multisafepay.git
    WORKDIR /pretix-multisafepay
    RUN pip3 install -e . && make
    USER pretixuser
    RUN cd /pretix/src && make production


License
-------



Released under the terms of the Apache License 2.0


.. _pretix: https://github.com/pretix/pretix
.. _pretix development setup: https://docs.pretix.eu/en/latest/development/setup.html
