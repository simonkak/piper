gnome = import('gnome')

desktopdir = join_paths(datadir, 'applications')
icondir = join_paths(datadir, 'icons', 'hicolor', 'scalable', 'apps')
symbolicicondir = join_paths(datadir, 'icons', 'hicolor', 'symbolic', 'apps')
metainfodir = join_paths(datadir, 'metainfo')
man1dir = join_paths(mandir, 'man1')

conf = configuration_data()
conf.set('version', meson.project_version())
conf.set('version_date', version_date)

manpage = configure_file(
     input: files('piper.1.in'),
     output: 'piper.1',
     configuration: conf,
)

install_man(
     manpage,
     install_dir: man1dir,
)

about_dialog = configure_file(input: 'ui/AboutDialog.ui.in',
                              output: 'AboutDialog.ui',
                              configuration: conf)

install_data('org.freedesktop.Piper.svg', install_dir: icondir)
install_data('org.freedesktop.Piper-symbolic.svg', install_dir: symbolicicondir)

i18n.merge_file(input: 'org.freedesktop.Piper.desktop.in',
                output: 'org.freedesktop.Piper.desktop',
                type: 'desktop',
                po_dir: podir,
                install: true,
                install_dir: desktopdir)

appdata = configure_file(input: 'org.freedesktop.Piper.appdata.xml.in.in',
                         output: 'org.freedesktop.Piper.appdata.xml.in',
                         configuration: conf)

appdata = i18n.merge_file(input: appdata,
                          output: 'org.freedesktop.Piper.appdata.xml',
                          type: 'xml',
                          po_dir: podir,
                          install: true,
                          install_dir: metainfodir)

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test(
    'validate appdata file',
    appstreamcli,
    args: ['validate', '--no-net', '--pedantic', appdata]
  )
endif

svg_mapping = files('svgs/svg-lookup.ini')

gresource = configure_file(input: 'piper.gresource.xml.in',
                           output: 'piper.gresource.xml',
                           command: ['generate-piper-gresource.xml.py',
                                     join_paths(meson.current_source_dir(), 'piper.gresource.xml.in'),
                                     join_paths(meson.current_build_dir(), 'piper.gresource.xml'),
                                     join_paths(meson.current_source_dir(), 'svgs')])

gnome.compile_resources('piper', gresource,
                        source_dir: '.',
                        dependencies: [about_dialog],
                        gresource_bundle: true,
                        install: true,
                        install_dir: pkgdatadir)

# ---- Per-application profile feature -------------------------------------
# KWin focus reporter (installed into the KWin script search path).
install_subdir('kwin/piper-appprofiles',
               install_dir: join_paths(datadir, 'kwin', 'scripts'))

# Example config shipped as documentation / a seed.
install_data('appprofiles.example.json', install_dir: pkgdatadir)

# systemd --user unit for the background agent.
agent_unit_conf = configuration_data()
agent_unit_conf.set('bindir', bindir)
configure_file(input: 'piper-profile-agent.service.in',
               output: 'piper-profile-agent.service',
               configuration: agent_unit_conf,
               install_dir: join_paths(datadir, 'systemd', 'user'))
