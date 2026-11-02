set(testrunner ${CMAKE_CURRENT_SOURCE_DIR}/run_test.sh)

add_test(NAME replay-one-fixture
         COMMAND ${testrunner} $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)

add_test(NAME replay-from-snapshot
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_resume.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-from-snapshot PROPERTIES TIMEOUT 120)

add_test(NAME replay-from-stopped-snapshot
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_stopped.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-from-stopped-snapshot PROPERTIES TIMEOUT 120)

add_test(NAME replay-stop-during-record
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_stop_recording.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-stop-during-record PROPERTIES TIMEOUT 120)

add_test(NAME replay-statesave-during-stop
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_stop_snapshot.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-statesave-during-stop PROPERTIES TIMEOUT 120)

add_test(NAME replay-media-key
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_media_key.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/fixture.prg)
set_tests_properties(replay-media-key PROPERTIES TIMEOUT 120)

add_test(NAME replay-media-mouse
         COMMAND python3 ${CMAKE_CURRENT_SOURCE_DIR}/run_media_mouse.py
                 $<TARGET_FILE:${APP_NAME}>
                 ${CMAKE_CURRENT_SOURCE_DIR}/mouse.prg)
set_tests_properties(replay-media-mouse PROPERTIES TIMEOUT 120)
